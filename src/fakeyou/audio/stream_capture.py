import asyncio
import base64
import binascii
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any, Protocol, override, runtime_checkable

from playwright.async_api import Error as PlaywrightError

from fakeyou.audio.audio import (
    DEFAULT_STREAM_POLICY,
    MeetingAudioError,
    MeetingAudioSession,
    SpeechSessionFactory,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    StreamSource,
    open_stream_audio,
)
from fakeyou.audio.deaf import DeafVerdict
from fakeyou.audio.graph import TurnAudioTap
from fakeyou.audio.own_speech import OwnSpeech
from fakeyou.audio.playback import PlaybackPort
from fakeyou.audio.stream_input import StreamAudioInput
from fakeyou.audio.voice_activity import MEET_AUDIO_SLOTS, SlotBinder
from fakeyou.config.settings import DEFAULTS
from fakeyou.foundation.invariants import POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.numbers import real_number
from fakeyou.foundation.polling import PageEvaluator, PollingLoop
from fakeyou.foundation.resources import load_bridged_script
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import (
    ConsumerSink,
    Fault,
    create_poll_guard,
    record_consumer_fault,
)
from fakeyou.observability.journal import Journal
from fakeyou.persistence.audio_persist import SlotAudioWriter

CAPTURE_DRAIN_JS = (
    "() => { const capture = window.__fakeyouStreamCapture;"
    " if (!capture) return null; return capture.drain(); }"
)

SESSION_SILENCE_SECONDS = 30.0
STREAM_FLEET_SLOTS = 4


@dataclass(frozen=True, slots=True)
class RegistryTiming:
    monotonic: Callable[[], float] = time.monotonic
    silence_seconds: float = SESSION_SILENCE_SECONDS


DEFAULT_REGISTRY_TIMING = RegistryTiming()


@dataclass(frozen=True, slots=True)
class DrainPolling:
    poll_interval_seconds: float = 0.25
    heartbeat_polls: int = 40
    max_consecutive_failures: int = DEFAULTS.audio.drain_max_consecutive_failures
    failure_journal_every: int = DEFAULTS.audio.drain_failure_journal_every


DEFAULT_DRAIN_POLLING = DrainPolling()


def load_capture_script() -> str:
    return load_bridged_script("stream_capture.js")


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


def _parse_count(entry: Mapping[str, Any], key: str) -> int:
    value = entry.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _drain_error(f"{key} is not a valid integer")
    return value


def _parse_csrcs(raw_csrcs: Any) -> tuple[int, ...]:
    if not isinstance(raw_csrcs, (list, tuple)):
        raise _drain_error("csrcs is not a list")
    csrcs: list[int] = []
    for csrc in raw_csrcs:
        if isinstance(csrc, bool) or not isinstance(csrc, int):
            raise _drain_error("csrc is not an integer")
        csrcs.append(csrc)
    return tuple(csrcs)


def _parse_number(entry: Mapping[str, Any], key: str) -> float:
    value = real_number(entry.get(key))
    if value is None:
        raise _drain_error(f"{key} is not a number")
    return value


def _parse_pcm(pcm_b64: Any) -> bytes:
    if not isinstance(pcm_b64, str):
        raise _drain_error("pcm_b64 is not a string")
    try:
        pcm = base64.b64decode(pcm_b64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise _drain_error("pcm_b64 is not base64") from error
    if len(pcm) % 2 != 0:
        raise _drain_error("pcm length is not int16 aligned")
    return pcm


def _parse_entry(entry: Any) -> StreamChunk:
    if not isinstance(entry, Mapping):
        raise _drain_error("entry is not a mapping")
    return StreamChunk(
        slot=_parse_count(entry, "slot"),
        seq=_parse_count(entry, "seq"),
        csrcs=_parse_csrcs(entry.get("csrcs")),
        level=_parse_number(entry, "level"),
        page_ms=_parse_number(entry, "page_ms"),
        pcm=_parse_pcm(entry.get("pcm_b64")),
    )


@dataclass(frozen=True, slots=True)
class DrainBatch:
    chunks: list[StreamChunk]
    skipped: int
    first_error: MeetingAudioError | None

    @property
    def error_type(self) -> str | None:
        if self.first_error is None:
            return None
        return type(self.first_error).__name__


def parse_drain(payload: Mapping[str, Any]) -> DrainBatch:
    if not isinstance(payload, Mapping):
        raise _drain_error("payload is not a mapping")
    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise _drain_error("streams is not a list")
    chunks: list[StreamChunk] = []
    skipped = 0
    first_error: MeetingAudioError | None = None
    for entry in streams:
        try:
            chunks.append(_parse_entry(entry))
        except MeetingAudioError as error:
            skipped += 1
            if first_error is None:
                first_error = error
    return DrainBatch(chunks=chunks, skipped=skipped, first_error=first_error)


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
    return StreamAudioInput()


class StreamRegistry:
    def __init__(
        self,
        *,
        timing: RegistryTiming = DEFAULT_REGISTRY_TIMING,
        input_factory: Callable[[], StreamAudioInput] | None = None,
        binder: SlotBinder | None = None,
        journal: Journal | None = None,
        recorder: SlotAudioWriter | None = None,
    ) -> None:
        self._monotonic = timing.monotonic
        self._input_factory = _default_input if input_factory is None else input_factory
        self._silence_seconds = timing.silence_seconds
        self._binder = binder
        self._journal = journal
        self.recorder = recorder
        self._sessions: dict[int, StreamSession] = {}

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
        if self.recorder is not None:
            try:
                self.recorder.write_chunk(chunk.slot, chunk.seq, chunk.page_ms, chunk.pcm)
            except (OSError, ValueError) as error:
                record_consumer_fault(
                    ConsumerSink(journal=self._journal),
                    source="audio",
                    detail="audio_persist",
                    error=error,
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


class CaptureDrainLoop(PollingLoop):
    def __init__(
        self,
        page: PageEvaluator,
        registry: StreamRegistry,
        *,
        polling: DrainPolling = DEFAULT_DRAIN_POLLING,
        sinks: ConsumerSink | None = None,
        twin_name: str | None = None,
    ) -> None:
        super().__init__(
            poll_interval_seconds=polling.poll_interval_seconds,
            guard=create_poll_guard(sinks, source="audio", operation="capture_drain"),
        )
        self._page = page
        self._registry = registry
        self._journal = sinks.journal if sinks is not None else None
        self._heartbeat_polls = polling.heartbeat_polls
        self._twin_name = twin_name
        self._faults = sinks.faults if sinks is not None else None
        self._max_consecutive_failures = polling.max_consecutive_failures
        self._failure_journal_every = polling.failure_journal_every
        self._polls = 0
        self._fails = 0
        self._consecutive_failures = 0
        self._failure: MeetingAudioError | None = None
        self._rescued_seen: dict[int, int] = {}

    def fail(self, message: str, *, cause: BaseException) -> None:
        if self._failure is not None:
            return
        self._failure = MeetingAudioError(message)
        if self._faults is not None:
            self._faults.record(Fault(source="audio", kind="unavailable", detail=message))
        if self._journal is not None:
            self._journal.record(
                "drain_poll_exhausted",
                source="transport",
                consecutive=self._consecutive_failures,
                error_type=type(cause).__name__,
            )

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _note_failure(self, stage: str, error: BaseException, *, skipped: int = 0) -> None:
        self._consecutive_failures += 1
        consecutive = self._consecutive_failures
        if self._journal is not None and (
            consecutive == 1 or consecutive % self._failure_journal_every == 0
        ):
            self._journal.record(
                "drain_poll_failed",
                source="transport",
                stage=stage,
                error_type=type(error).__name__,
                consecutive=consecutive,
                skipped=skipped,
            )
        if consecutive >= self._max_consecutive_failures:
            self.fail(f"capture drain failed {consecutive} times in a row", cause=error)

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

    async def _fetch_batch(self) -> Mapping[str, Any] | None:
        try:
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                batch = await self._page.evaluate(CAPTURE_DRAIN_JS)
            if not isinstance(batch, Mapping):
                raise MeetingAudioError("capture batch is not a mapping")
        except (MeetingAudioError, PlaywrightError, TimeoutError) as error:
            self._fails += 1
            self._note_failure("evaluate", error)
            return None
        else:
            return batch

    async def _feed_chunks(self, chunks: list[StreamChunk]) -> bool:
        failed = False
        for chunk in chunks:
            try:
                await self._registry.feed(chunk)
            except RuntimeError as error:
                failed = True
                self._note_failure("feed", error)
        return failed

    def _settle_batch(self, batch: Mapping[str, Any], parsed: DrainBatch) -> None:
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
        self._heartbeat(batch, parsed.chunks)

    async def poll(self) -> bool:
        if self._failure is not None:
            return False
        batch = await self._fetch_batch()
        if batch is None:
            return False
        try:
            parsed = parse_drain(batch)
        except MeetingAudioError as error:
            self._fails += 1
            self._note_failure("parse", error)
            return False
        failed = await self._feed_chunks(parsed.chunks)
        self._settle_batch(batch, parsed)
        if parsed.skipped and parsed.first_error is not None:
            failed = True
            self._note_failure("parse", parsed.first_error, skipped=parsed.skipped)
        if not failed:
            self._consecutive_failures = 0
        return True

    @override
    async def _poll_once(self) -> None:
        await self.poll()

    @override
    def _record_poll_failure(self, error: Exception) -> None:
        self._fails += 1
        self._note_failure("poll", error)


async def run_fleet_calls(calls: Iterable[Callable[[], Awaitable[None]]], operation: str) -> None:
    errors: list[Exception] = []

    async def attempt(call: Callable[[], Awaitable[None]]) -> None:
        try:
            await call()
        except (RuntimeError, OSError) as error:
            errors.append(error)

    async with asyncio.TaskGroup() as group:
        for call in calls:
            group.create_task(attempt(call))
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise MeetingAudioError(f"fleet {operation} failed") from errors[0]


async def tick_slot(
    session: MeetingAudioSession, *, is_busy: bool, remote_participants: int | None
) -> DeafVerdict | Exception:
    try:
        return await session.maintenance_tick(
            is_busy=is_busy, remote_participants=remote_participants
        )
    except (MeetingAudioError, OSError) as error:
        return error


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
        language: str | None = None,
    ) -> None:
        await self._voice().speak(
            text,
            sentence_index=sentence_index,
            transcript_id=transcript_id,
            language=language,
        )

    async def stop_playback(self) -> None:
        await run_fleet_calls(
            [session.stop_playback for session in self._sessions],
            "stop_playback",
        )

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

    def update_keywords(self, terms: Sequence[str]) -> bool:
        changed = [session.update_keywords(terms) for session in self._sessions]
        return any(changed)

    async def maintenance_tick(
        self, *, is_busy: bool, remote_participants: int | None = None
    ) -> DeafVerdict:
        async with asyncio.TaskGroup() as group:
            tasks = [
                group.create_task(
                    tick_slot(session, is_busy=is_busy, remote_participants=remote_participants)
                )
                for session in self._sessions
            ]
        verdicts: list[DeafVerdict] = []
        for task in tasks:
            result = task.result()
            if isinstance(result, MeetingAudioError):
                continue
            if isinstance(result, Exception):
                raise result
            verdicts.append(result)
        if verdicts:
            return verdicts[0]
        self.raise_if_failed()
        raise MeetingAudioError("stream fleet is exhausted")

    async def stop(self) -> None:
        await run_fleet_calls([session.stop for session in self._sessions], "stop")


@dataclass(frozen=True, slots=True)
class FleetTopology:
    binder: SlotBinder
    slots: int = MEET_AUDIO_SLOTS
    recorder: SlotAudioWriter | None = None


@dataclass(frozen=True, slots=True)
class SlotFactories:
    stt: Callable[[int], SpeechSessionFactory] | None = None
    playback: Callable[[int], PlaybackPort] | None = None
    turn_tap: Callable[[int], TurnAudioTap] | None = None


DEFAULT_SLOT_FACTORIES = SlotFactories()


@asynccontextmanager
async def open_stream_fleet(
    *,
    topology: FleetTopology,
    callbacks: StreamCallbacks,
    factories: SlotFactories = DEFAULT_SLOT_FACTORIES,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    policy: StreamPolicy = DEFAULT_STREAM_POLICY,
) -> AsyncIterator[StreamFleetSession]:
    if topology.slots <= 0:
        raise ValueError("stream fleet needs at least one slot")
    registry = StreamRegistry(
        binder=topology.binder, journal=diagnostics.journal, recorder=topology.recorder
    )
    own_speech = OwnSpeech()
    async with AsyncExitStack() as stack:
        sessions: list[MeetingAudioSession] = []
        for slot in range(topology.slots):
            streamer = StreamAudioInput()
            registry.register(slot, streamer)
            slot_callbacks = callbacks if slot == 0 else replace(callbacks, on_playback_event=None)
            sessions.append(
                await stack.enter_async_context(
                    open_stream_audio(
                        source=StreamSource(f"slot-{slot}", streamer),
                        callbacks=slot_callbacks,
                        components=StreamComponents(
                            stt=factories.stt(slot) if factories.stt is not None else None,
                            playback=(
                                factories.playback(slot) if factories.playback is not None else None
                            ),
                            turn_tap=(
                                factories.turn_tap(slot) if factories.turn_tap is not None else None
                            ),
                            own_speech=own_speech,
                        ),
                        diagnostics=diagnostics,
                        policy=policy,
                    )
                )
            )
        yield StreamFleetSession(sessions, registry)
