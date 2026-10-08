import asyncio
import base64
import binascii
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol, runtime_checkable

from pipecat.frames.frames import InputAudioRawFrame, StartFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams

from fakeyou.audio import MeetingAudioError, MeetingAudioSession, TurnAudioTap, open_stream_audio
from fakeyou.audio_persist import SlotAudioWriter
from fakeyou.deaf import DeafVerdict
from fakeyou.faults import FaultReporter, record_consumer_fault
from fakeyou.journal import Journal
from fakeyou.playback import PlaybackEngine, PlaybackEvent
from fakeyou.settings import DEFAULTS, STTSettings, TTSSettings
from fakeyou.voice_activity import DEPARTURE_GRACE_MS, MEET_AUDIO_SLOTS, SlotBinder

CAPTURE_DRAIN_JS = (
    "() => { const capture = window.__fakeyouStreamCapture;"
    " if (!capture) return null; return capture.drain(); }"
)

SESSION_SILENCE_SECONDS = 30.0
EARLY_AUDIO_BYTES = 160000
STREAM_FLEET_SLOTS = 4


def load_capture_script() -> str:
    return (files("fakeyou") / "assets" / "stream_capture.js").read_text(encoding="utf-8")


class StreamAudioInput(BaseInputTransport):
    def __init__(self, params: TransportParams, **kwargs: Any) -> None:
        super().__init__(params, **kwargs)
        self._early: list[InputAudioRawFrame] = []
        self._early_bytes = 0
        self._capture: tuple[int, float] | None = None

    def note_capture(self, seq: int, capture_ms: float) -> None:
        self._capture = (seq, capture_ms)

    @property
    def last_capture(self) -> tuple[int, float] | None:
        return self._capture

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)
        early, self._early = self._early, []
        self._early_bytes = 0
        for buffered in early:
            await self.push_audio_frame(buffered)

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        frame = InputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=1)
        if getattr(self, "_audio_in_queue", None) is None:
            self._early.append(frame)
            self._early_bytes += len(audio)
            while self._early and self._early_bytes > EARLY_AUDIO_BYTES:
                dropped = self._early.pop(0)
                self._early_bytes -= len(dropped.audio)
            return
        await self.push_audio_frame(frame)


@dataclass(frozen=True)
class StreamChunk:
    slot: int
    seq: int
    csrcs: tuple[int, ...]
    level: float
    page_ms: float
    pcm: bytes


@dataclass
class StreamSession:
    slot: int
    input: StreamAudioInput
    seq: int = -1
    csrcs: tuple[int, ...] = ()
    level: float = 0.0
    page_ms: float = 0.0
    last_seen: float = 0.0
    pinned: bool = False


def _drain_error(detail: str) -> MeetingAudioError:
    return MeetingAudioError(f"stream drain window is corrupt: {detail}")


def _parse_entry(entry: Any) -> StreamChunk:
    if not isinstance(entry, Mapping):
        raise _drain_error("entry is not a mapping")
    slot = entry.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise _drain_error("slot is not a valid integer")
    seq = entry.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise _drain_error("seq is not a valid integer")
    raw_csrcs = entry.get("csrcs")
    if not isinstance(raw_csrcs, (list, tuple)):
        raise _drain_error("csrcs is not a list")
    csrcs: list[int] = []
    for csrc in raw_csrcs:
        if isinstance(csrc, bool) or not isinstance(csrc, int):
            raise _drain_error("csrc is not an integer")
        csrcs.append(csrc)
    level = entry.get("level")
    if isinstance(level, bool) or not isinstance(level, (int, float)):
        raise _drain_error("level is not a number")
    page_ms = entry.get("page_ms")
    if isinstance(page_ms, bool) or not isinstance(page_ms, (int, float)):
        raise _drain_error("page_ms is not a number")
    pcm_b64 = entry.get("pcm_b64")
    if not isinstance(pcm_b64, str):
        raise _drain_error("pcm_b64 is not a string")
    try:
        pcm = base64.b64decode(pcm_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _drain_error("pcm_b64 is not base64") from error
    if len(pcm) % 2 != 0:
        raise _drain_error("pcm length is not int16 aligned")
    return StreamChunk(
        slot=slot,
        seq=seq,
        csrcs=tuple(csrcs),
        level=float(level),
        page_ms=float(page_ms),
        pcm=pcm,
    )


def parse_drain(payload: Mapping[str, Any]) -> list[StreamChunk]:
    if not isinstance(payload, Mapping):
        raise _drain_error("payload is not a mapping")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise _drain_error("streams is not a list")
    return [_parse_entry(entry) for entry in streams]


def parse_roster(payload: Mapping[str, Any], *, twin_name: str | None = None) -> tuple[str, ...]:
    roster = payload.get("roster")
    if not isinstance(roster, list):
        return ()
    folded = twin_name.strip().casefold() if isinstance(twin_name, str) else ""
    found: list[tuple[str, Any]] = []
    for entry in roster:
        if not isinstance(entry, Mapping):
            continue
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        found.append((ident, entry.get("name")))
    if folded:
        matches = [
            ident
            for ident, name in found
            if isinstance(name, str) and name.strip().casefold() == folded
        ]
        if len(matches) == 1:
            return tuple(ident for ident, _ in found if ident != matches[0])
    return tuple(ident for ident, _ in found)


def parse_roster_names(
    payload: Mapping[str, Any], *, twin_name: str | None = None
) -> tuple[dict[str, str], ...]:
    roster = payload.get("roster")
    if not isinstance(roster, list):
        return ()
    folded = twin_name.strip().casefold() if isinstance(twin_name, str) else ""
    found: list[tuple[str, str]] = []
    for entry in roster:
        if not isinstance(entry, Mapping):
            continue
        ident = entry.get("id")
        name = entry.get("name")
        if not isinstance(ident, str) or not ident:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        found.append((ident, name.strip()))
    if folded:
        matches = [ident for ident, name in found if name.casefold() == folded]
        if len(matches) == 1:
            return tuple(
                {"id": ident, "name": name} for ident, name in found if ident != matches[0]
            )
    return tuple({"id": ident, "name": name} for ident, name in found)


@dataclass(frozen=True)
class TapHealth:
    slot: int
    stalled: bool
    rescued: int
    decode_fresh: bool
    engine: str


def parse_health(payload: Mapping[str, Any]) -> dict[int, TapHealth]:
    if not isinstance(payload, Mapping):
        return {}
    raw = payload.get("health")
    if not isinstance(raw, list):
        return {}
    found: dict[int, TapHealth] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        slot = entry.get("slot")
        stalled = entry.get("stalled")
        rescued = entry.get("rescued")
        fresh = entry.get("decode_fresh")
        engine = entry.get("engine")
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or not isinstance(stalled, bool)
            or isinstance(rescued, bool)
            or not isinstance(rescued, int)
            or rescued < 0
            or not isinstance(fresh, bool)
            or not isinstance(engine, str)
        ):
            continue
        found[slot] = TapHealth(
            slot=slot, stalled=stalled, rescued=rescued, decode_fresh=fresh, engine=engine
        )
    return found


def parse_released(payload: Mapping[str, Any]) -> tuple[int, ...]:
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("released")
    if not isinstance(raw, list):
        return ()
    found: list[int] = []
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry < 0:
            continue
        found.append(entry)
    return tuple(found)


def _default_input() -> StreamAudioInput:
    return StreamAudioInput(TransportParams(audio_in_enabled=True))


class StreamRegistry:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        input_factory: Callable[[], StreamAudioInput] | None = None,
        silence_seconds: float = SESSION_SILENCE_SECONDS,
        binder: SlotBinder | None = None,
        journal: Journal | None = None,
        recorder: SlotAudioWriter | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._input_factory = _default_input if input_factory is None else input_factory
        self._silence_seconds = silence_seconds
        self._binder = binder
        self._journal = journal
        self._recorder = recorder
        self._sessions: dict[int, StreamSession] = {}
        self._roster_seen: dict[str, float] = {}

    @property
    def sessions(self) -> Mapping[int, StreamSession]:
        return self._sessions

    @property
    def binder(self) -> SlotBinder | None:
        return self._binder

    def register(self, slot: int, audio: StreamAudioInput) -> None:
        self._sessions[slot] = StreamSession(
            slot=slot, input=audio, pinned=True, last_seen=self._monotonic()
        )

    def update_roster(self, devices: Iterable[str]) -> None:
        now = self._monotonic()
        for device in devices:
            self._roster_seen[device] = now
        grace = DEPARTURE_GRACE_MS / 1000.0
        for device in [name for name, seen in self._roster_seen.items() if now - seen > grace]:
            del self._roster_seen[device]
        if self._binder is not None:
            self._binder.update_roster(self._roster_seen)

    def note_roster(self, entries: Iterable[Mapping[str, str]]) -> None:
        if self._binder is not None:
            self._binder.note_roster(entries)

    async def feed(self, chunk: StreamChunk) -> None:
        session = self._sessions.get(chunk.slot)
        if session is None:
            session = StreamSession(slot=chunk.slot, input=self._input_factory())
            self._sessions[chunk.slot] = session
        expected = session.seq + 1
        if session.seq >= 0 and chunk.seq != expected and self._journal is not None:
            if chunk.seq > expected:
                self._journal.record(
                    "drain_gap_detected",
                    source="transport",
                    slot=chunk.slot,
                    kind="gap",
                    expected=expected,
                    received=chunk.seq,
                    missed=chunk.seq - expected,
                )
            else:
                self._journal.record(
                    "drain_gap_detected",
                    source="transport",
                    slot=chunk.slot,
                    kind="resync",
                    expected=expected,
                    received=chunk.seq,
                    missed=0,
                )
        session.seq = chunk.seq
        session.csrcs = chunk.csrcs
        session.level = chunk.level
        session.page_ms = chunk.page_ms
        session.last_seen = self._monotonic()
        if self._binder is not None:
            self._binder.observe_stream(chunk.slot, chunk.csrcs)
        session.input.note_capture(chunk.seq, chunk.page_ms)
        await session.input.feed(chunk.pcm)
        if self._recorder is not None:
            try:
                self._recorder.write_chunk(chunk.slot, chunk.seq, chunk.page_ms, chunk.pcm)
            except Exception as error:
                record_consumer_fault(
                    None, self._journal, source="audio", detail="audio_persist", error=error
                )

    def prune(self) -> None:
        now = self._monotonic()
        stale = [
            slot
            for slot, session in self._sessions.items()
            if not session.pinned and now - session.last_seen >= self._silence_seconds
        ]
        for slot in stale:
            del self._sessions[slot]


@runtime_checkable
class SupportsCaptureRegistry(Protocol):
    @property
    def capture_registry(self) -> StreamRegistry: ...


class CaptureDrainLoop:
    def __init__(
        self,
        page: Any,
        registry: StreamRegistry,
        *,
        poll_interval_seconds: float = 0.25,
        journal: Journal | None = None,
        heartbeat_polls: int = 40,
        twin_name: str | None = None,
    ) -> None:
        self._page = page
        self._registry = registry
        self._poll_interval = poll_interval_seconds
        self._journal = journal
        self._heartbeat_polls = heartbeat_polls
        self._twin_name = twin_name
        self._polls = 0
        self._fails = 0
        self._rescued_seen: dict[int, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def _heartbeat(self, batch: Mapping[str, Any], chunks: list[StreamChunk]) -> None:
        if self._journal is None or self._heartbeat_polls <= 0:
            return
        if self._polls % self._heartbeat_polls != 0:
            return
        tracks: dict[int, str] = {}
        engines: dict[int, str] = {}
        raw = batch.get("streams")
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, Mapping):
                    continue
                slot = entry.get("slot")
                track = entry.get("track")
                if isinstance(slot, int) and isinstance(track, str):
                    tracks[slot] = track
                engine = entry.get("engine")
                if isinstance(slot, int) and isinstance(engine, str):
                    engines[slot] = engine
        health = parse_health(batch)
        slots = {
            str(chunk.slot): {
                "level": chunk.level,
                "bytes": len(chunk.pcm),
                "track": tracks.get(chunk.slot, ""),
                "engine": engines.get(chunk.slot, ""),
                "stalled": health[chunk.slot].stalled if chunk.slot in health else False,
                "rescued": health[chunk.slot].rescued if chunk.slot in health else 0,
                "decode_fresh": health[chunk.slot].decode_fresh if chunk.slot in health else False,
            }
            for chunk in chunks
        }
        dropped = batch.get("dropped")
        roster = batch.get("roster")
        pre_filter = len(roster) if isinstance(roster, list) else 0
        tiles = batch.get("roster_tiles")
        if isinstance(tiles, bool) or not isinstance(tiles, int) or tiles < 0:
            tiles = pre_filter
        self._journal.record(
            "capture_polled",
            source="browser",
            slots=slots,
            dropped=dropped if isinstance(dropped, int) else 0,
            fails=self._fails,
            roster_tiles=tiles,
            roster_pre_filter=pre_filter,
            roster_post_filter=len(parse_roster(batch, twin_name=self._twin_name)),
        )

    async def poll(self) -> bool:
        try:
            batch = await self._page.evaluate(CAPTURE_DRAIN_JS)
            if not isinstance(batch, Mapping):
                self._fails += 1
                return False
            self._registry.update_roster(parse_roster(batch, twin_name=self._twin_name))
            self._registry.note_roster(parse_roster_names(batch, twin_name=self._twin_name))
            chunks = parse_drain(batch)
            for chunk in chunks:
                await self._registry.feed(chunk)
            for slot, health in parse_health(batch).items():
                if health.rescued > self._rescued_seen.get(slot, 0):
                    self._rescued_seen[slot] = health.rescued
                    if self._journal is not None:
                        self._journal.record(
                            "tap_slot_rescued",
                            source="transport",
                            slot=slot,
                            rescued=health.rescued,
                            stalled=health.stalled,
                        )
            if self._journal is not None:
                for slot in parse_released(batch):
                    self._journal.record(
                        "tap_slot_dropped",
                        source="transport",
                        slot=slot,
                    )
            self._registry.prune()
            self._polls += 1
            self._heartbeat(batch, chunks)
            return True
        except Exception:
            self._fails += 1
            return False

    async def start(self) -> None:
        if self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        self._closed = True
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _poll_loop(self) -> None:
        while True:
            await self.poll()
            await asyncio.sleep(self._poll_interval)


class StreamFleetSession:
    def __init__(self, sessions: Sequence[MeetingAudioSession], registry: StreamRegistry) -> None:
        if not sessions:
            raise ValueError("stream fleet needs at least one session")
        self._sessions = list(sessions)
        self._registry = registry

    @property
    def capture_registry(self) -> StreamRegistry:
        return self._registry

    @property
    def is_speaking(self) -> bool:
        return any(session.is_speaking for session in self._sessions)

    def _voice(self) -> MeetingAudioSession:
        for session in self._sessions:
            try:
                session.raise_if_failed()
            except MeetingAudioError:
                continue
            return session
        self.raise_if_failed()
        raise MeetingAudioError("stream fleet is exhausted")

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        await self._voice().speak(text, sentence_index=sentence_index, transcript_id=transcript_id)

    async def stop_playback(self) -> None:
        await asyncio.gather(*(session.stop_playback() for session in self._sessions))

    def raise_if_failed(self) -> None:
        failures: list[MeetingAudioError] = []
        for session in self._sessions:
            try:
                session.raise_if_failed()
            except MeetingAudioError as error:
                failures.append(error)
            else:
                return
        raise failures[0]

    async def wait_failed(self) -> None:
        await asyncio.gather(
            *(session.wait_failed() for session in self._sessions), return_exceptions=True
        )
        self.raise_if_failed()

    def mark_admitted(self) -> None:
        for session in self._sessions:
            session.mark_admitted()

    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> DeafVerdict:
        verdicts: list[DeafVerdict] = []
        for session in self._sessions:
            try:
                verdicts.append(
                    await session.maintenance_tick(
                        is_busy=is_busy, remote_participants=remote_participants
                    )
                )
            except MeetingAudioError:
                continue
        if verdicts:
            if all(verdict == "alone" for verdict in verdicts):
                return "alone"
            for verdict in verdicts:
                if verdict != "alone":
                    return verdict
        self.raise_if_failed()
        raise MeetingAudioError("stream fleet is exhausted")

    async def stop(self) -> None:
        await asyncio.gather(*(session.stop() for session in self._sessions))


@asynccontextmanager
async def open_stream_fleet(
    *,
    slots: int = MEET_AUDIO_SLOTS,
    binder: SlotBinder,
    on_transcript: Callable[[TranscriptionFrame], Awaitable[None]],
    on_interim: Callable[[TranscriptionFrame], Awaitable[None]] | None = None,
    on_vad: Callable[[bool], Awaitable[None]] | None = None,
    stt_factory: Callable[[int], FrameProcessor] | None = None,
    playback_factory: Callable[[int], PlaybackEngine] | None = None,
    turn_tap_factory: Callable[[int], TurnAudioTap] | None = None,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    recorder: SlotAudioWriter | None = None,
    deaf_window_seconds: float = DEFAULTS.audio_health.deaf_window_seconds,
    deaf_grace_seconds: float = DEFAULTS.audio_health.deaf_grace_seconds,
    alone_leave_seconds: float = DEFAULTS.audio_health.alone_leave_seconds,
    stt_settings: STTSettings = DEFAULTS.stt,
    stt_keywords: Sequence[str] | None = None,
    tts_settings: TTSSettings = DEFAULTS.tts,
    speech_language: str = DEFAULTS.speech.language,
    on_playback_event: Callable[[PlaybackEvent], None] | None = None,
) -> AsyncIterator[StreamFleetSession]:
    if slots <= 0:
        raise ValueError("stream fleet needs at least one slot")
    registry = StreamRegistry(binder=binder, journal=journal, recorder=recorder)
    async with AsyncExitStack() as stack:
        sessions: list[MeetingAudioSession] = []
        for slot in range(slots):
            streamer = StreamAudioInput(TransportParams(audio_in_enabled=True))
            registry.register(slot, streamer)
            sessions.append(
                await stack.enter_async_context(
                    open_stream_audio(
                        stream_id=f"slot-{slot}",
                        input=streamer,
                        on_transcript=on_transcript,
                        on_interim=on_interim,
                        on_vad=on_vad,
                        turn_tap=turn_tap_factory(slot) if turn_tap_factory is not None else None,
                        stt=stt_factory(slot) if stt_factory is not None else None,
                        playback=playback_factory(slot) if playback_factory is not None else None,
                        on_playback_event=on_playback_event if slot == 0 else None,
                        faults=faults,
                        journal=journal,
                        deaf_window_seconds=deaf_window_seconds,
                        deaf_grace_seconds=deaf_grace_seconds,
                        alone_leave_seconds=alone_leave_seconds,
                        stt_settings=stt_settings,
                        stt_keywords=stt_keywords,
                        tts_settings=tts_settings,
                        speech_language=speech_language,
                    )
                )
            )
        yield StreamFleetSession(sessions, registry)
