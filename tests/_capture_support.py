import asyncio
from collections.abc import Callable
from typing import override

import pytest
from pipecat.frames.frames import AudioRawFrame, Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.base_transport import TransportParams

from fakeyou.audio.audio import MeetingAudioSession
from fakeyou.audio.stream_capture import (
    RegistryTiming,
    StreamAudioInput,
    StreamRegistry,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.observability.journal import Journal


class EchoSTT(FrameProcessor):
    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
            await self.push_frame(
                TranscriptionFrame(self._text, "guest", "", finalized=True), direction
            )
        await self.push_frame(frame, direction)


class FakePlayback:
    def __init__(self) -> None:
        self.closed = False

    @property
    def is_speaking(self) -> bool:
        return False

    async def speak(self, text: str, **extra: object) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _RecordingInput(StreamAudioInput):
    def __init__(self) -> None:
        super().__init__(TransportParams(audio_in_enabled=True))
        self.fed: list[bytes] = []
        self._capture: tuple[int, float] | None = None

    @override
    def note_capture(self, seq: int, capture_ms: float) -> None:
        self._capture = (seq, capture_ms)

    @property
    @override
    def last_capture(self) -> tuple[int, float] | None:
        return self._capture

    @override
    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        self.fed.append(audio)


def recording_registry(
    monotonic: Callable[[], float],
    binder: SlotBinder | None = None,
    journal: Journal | None = None,
) -> tuple[StreamRegistry, list[_RecordingInput]]:
    made: list[_RecordingInput] = []

    def _factory() -> StreamAudioInput:
        created = _RecordingInput()
        made.append(created)
        return created

    registry = StreamRegistry(
        timing=RegistryTiming(monotonic),
        input_factory=_factory,
        binder=binder,
        journal=journal,
    )
    return registry, made


async def settle_reconnect(session: MeetingAudioSession) -> None:
    for _ in range(100):
        if session.reconnect_state() != "running":
            return
        await asyncio.sleep(0.01)
    pytest.fail("reconnect task did not settle")
