import asyncio
from collections.abc import Callable
from typing import override

import pytest

from fakeyou.audio.audio import MeetingAudioSession
from fakeyou.audio.stream_capture import RegistryTiming, StreamRegistry
from fakeyou.audio.stream_input import StreamAudioInput
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.observability.journal import Journal
from tests._audio_fakes import FakeSTT


def echo_stt(text: str) -> FakeSTT:
    return FakeSTT(text, finalized=(True,))


class FakePlayback:
    def __init__(self) -> None:
        self.closed = False

    @property
    def is_speaking(self) -> bool:
        return False

    def louder(self) -> None:
        pass

    async def speak(self, text: str, **extra: object) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _RecordingInput(StreamAudioInput):
    def __init__(self) -> None:
        super().__init__()
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
