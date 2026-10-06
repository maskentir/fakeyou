import asyncio
import base64
import binascii
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams

from fakeyou.audio import MeetingAudioError

CAPTURE_DRAIN_JS = (
    "() => { const capture = window.__fakeyouStreamCapture;"
    " if (!capture) return null; return capture.drain(); }"
)

SESSION_SILENCE_SECONDS = 30.0


def load_capture_script() -> str:
    return (files("fakeyou") / "assets" / "stream_capture.js").read_text(encoding="utf-8")


class StreamAudioInput(BaseInputTransport):
    def __init__(self, params: TransportParams, **kwargs: Any) -> None:
        super().__init__(params, **kwargs)

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        await self.push_audio_frame(
            InputAudioRawFrame(audio=audio, sample_rate=sample_rate, num_channels=1)
        )


@dataclass(frozen=True)
class StreamChunk:
    slot: int
    csrcs: tuple[int, ...]
    level: float
    page_ms: float
    pcm: bytes


@dataclass
class StreamSession:
    slot: int
    input: StreamAudioInput
    csrcs: tuple[int, ...] = ()
    level: float = 0.0
    page_ms: float = 0.0
    last_seen: float = 0.0


def _drain_error(detail: str) -> MeetingAudioError:
    return MeetingAudioError(f"stream drain window is corrupt: {detail}")


def _parse_entry(entry: Any) -> StreamChunk:
    if not isinstance(entry, Mapping):
        raise _drain_error("entry is not a mapping")
    slot = entry.get("slot")
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
        raise _drain_error("slot is not a valid integer")
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


def _default_input() -> StreamAudioInput:
    return StreamAudioInput(TransportParams(audio_in_enabled=True))


class StreamRegistry:
    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        input_factory: Callable[[], StreamAudioInput] | None = None,
        silence_seconds: float = SESSION_SILENCE_SECONDS,
    ) -> None:
        self._monotonic = monotonic
        self._input_factory = _default_input if input_factory is None else input_factory
        self._silence_seconds = silence_seconds
        self._sessions: dict[int, StreamSession] = {}

    @property
    def sessions(self) -> Mapping[int, StreamSession]:
        return self._sessions

    async def feed(self, chunk: StreamChunk) -> None:
        session = self._sessions.get(chunk.slot)
        if session is None:
            session = StreamSession(slot=chunk.slot, input=self._input_factory())
            self._sessions[chunk.slot] = session
        session.csrcs = chunk.csrcs
        session.level = chunk.level
        session.page_ms = chunk.page_ms
        session.last_seen = self._monotonic()
        await session.input.feed(chunk.pcm)

    def prune(self) -> None:
        now = self._monotonic()
        stale = [
            slot
            for slot, session in self._sessions.items()
            if now - session.last_seen >= self._silence_seconds
        ]
        for slot in stale:
            del self._sessions[slot]


class CaptureDrainLoop:
    def __init__(
        self,
        page: Any,
        registry: StreamRegistry,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self._page = page
        self._registry = registry
        self._poll_interval = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    async def poll(self) -> bool:
        try:
            batch = await self._page.evaluate(CAPTURE_DRAIN_JS)
            if not isinstance(batch, Mapping):
                return False
            for chunk in parse_drain(batch):
                await self._registry.feed(chunk)
            self._registry.prune()
            return True
        except Exception:
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
