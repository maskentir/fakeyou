import asyncio
from collections.abc import AsyncIterator
from typing import Any, TypedDict, Unpack, cast

import numpy as np

from fakeyou.audio.audio import SessionRun
from fakeyou.audio.graph import AudioGraph, PcmQueue
from fakeyou.audio.playback import (
    PlaybackEngine,
    PlaybackEvent,
    PlaybackPort,
    PlaybackTiming,
)
from fakeyou.audio.vad import VadAnalyzer, VadController, VadParams
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.speech.contract import SpeechSynthesizer
from tests._persistence_fakes import RecordingJournal

SPEECH_WINDOW = b"\x00\x00" * 512
SPEAKING = 0.99
SILENT = 0.0


class ScriptedSpeechModel:
    def __init__(self) -> None:
        self.probability = SILENT

    def predict(self, window: np.ndarray) -> float:
        del window
        return self.probability

    def reset(self) -> None:
        return None


class ScriptedSpeech:
    def __init__(self) -> None:
        self.model = ScriptedSpeechModel()
        analyzer = VadAnalyzer(
            VadParams(min_volume=0.0), model=self.model, model_reset_samples=None
        )
        self.controller = VadController(analyzer, audio_idle_timeout_seconds=0)

    def speaking(self, windows: int = 8) -> bytes:
        self.model.probability = SPEAKING
        return SPEECH_WINDOW * windows

    def silent(self, windows: int = 8) -> bytes:
        self.model.probability = SILENT
        return SPEECH_WINDOW * windows


class NullSTT:
    def __init__(self) -> None:
        self.received = 0

    def send_audio(self, pcm: bytes) -> None:
        self.received += len(pcm)


def build_run(engine: PlaybackPort, task: asyncio.Task[None]) -> SessionRun:
    return SessionRun(
        graph=cast(AudioGraph, object()),
        task=task,
        source=FakeCapture(),
        engine=engine,
    )


async def wait_gate(gate: asyncio.Event) -> None:
    await gate.wait()


class FakeCapture:
    def __init__(self) -> None:
        self.queue = PcmQueue()
        self.started = 0
        self.terminated = 0
        self.backend: Any = None

    async def start(self) -> None:
        self.started += 1

    def terminate(self) -> None:
        self.terminated += 1
        if self.backend is not None:
            self.backend.terminate()

    def push(self, pcm: bytes) -> None:
        self.queue.put(pcm)


class FakePlayback:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.stops = 0
        self.closed = False
        self.speaking = False
        self.louder_calls = 0

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    def louder(self) -> None:
        self.louder_calls += 1

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        del language
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True


class FakeSynth:
    def __init__(self, audio: bytes = b"\x00" * 480, error: Exception | None = None) -> None:
        self.audio = audio
        self.error = error
        self.texts: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.closed = False

    async def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        del language
        self.texts.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        if self.error is not None:
            raise self.error
        yield self.audio

    async def aclose(self) -> None:
        self.closed = True


class FakeWriter:
    def __init__(self, error: Exception | None = None, buffer_seconds: float = 0.0) -> None:
        self.error = error
        self.buffer_seconds = buffer_seconds
        self.written: list[bytes] = []
        self.cancelled: list[bytes] = []
        self.drains = 0
        self.closed = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    @property
    def heard(self) -> bytes:
        return b"".join(self.written)

    async def write(self, pcm: bytes) -> None:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(pcm)
            raise
        if self.error is not None:
            raise self.error
        self.written.append(pcm)

    async def drain(self) -> None:
        self.drains += 1

    async def close(self) -> None:
        self.closed = True


class EngineScript(TypedDict, total=False):
    journal: RecordingJournal
    faults: FaultReporter
    cleanup_timeout_seconds: float


def build_engine(
    synth: SpeechSynthesizer,
    writer: FakeWriter,
    events: list[PlaybackEvent],
    **script: Unpack[EngineScript],
) -> PlaybackEngine:
    return PlaybackEngine(
        synth,
        writer,
        on_event=events.append,
        timing=PlaybackTiming(cleanup_timeout_seconds=script.get("cleanup_timeout_seconds", 5.0)),
        diagnostics=Diagnostics(faults=script.get("faults"), journal=script.get("journal")),
    )
