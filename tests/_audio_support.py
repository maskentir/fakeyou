import asyncio
from collections.abc import AsyncIterator
from typing import Any, TypedDict, Unpack, cast, override

from pipecat.frames.frames import Frame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from fakeyou.audio.audio import SessionPipeline
from fakeyou.audio.playback import (
    PlaybackBackend,
    PlaybackEngine,
    PlaybackEvent,
    PlaybackPort,
    PlaybackTiming,
    PlayStream,
    SetMuted,
    SpeechSynthesizer,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal


def build_pipeline(engine: PlaybackPort, runner_task: asyncio.Task[None]) -> SessionPipeline:
    worker = cast(PipelineWorker, object())
    runner = cast(WorkerRunner, object())
    return SessionPipeline(worker, runner, runner_task, engine)


async def wait_gate(gate: asyncio.Event) -> None:
    await gate.wait()


class FakeTransport:
    def __init__(self) -> None:
        self.source = FakeInput()
        self.backend: Any = None

    def input(self) -> FrameProcessor:
        return self.source

    def terminate(self) -> None:
        if self.backend is not None:
            self.backend.terminate()


class FakePlayback:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.stops = 0
        self.closed = False
        self.speaking = False

    @property
    def is_speaking(self) -> bool:
        return self.speaking

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> None:
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)

    async def stop(self) -> None:
        self.stops += 1

    async def close(self) -> None:
        self.closed = True


class FakeInput(FrameProcessor):
    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class FakeSynth:
    def __init__(self, audio: bytes = b"\x00" * 480, error: Exception | None = None) -> None:
        self.audio = audio
        self.error = error
        self.texts: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.closed = False

    async def stream(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ) -> AsyncIterator[bytes]:
        self.texts.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)
        if self.error is not None:
            raise self.error
        yield self.audio

    async def aclose(self) -> None:
        self.closed = True


class FakePlayer:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.played: list[bytes] = []
        self.cancelled: list[bytes] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, chunks: AsyncIterator[bytes]) -> None:
        data = b"".join([chunk async for chunk in chunks])
        self.played.append(data)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(data)
            raise
        if self.error is not None:
            raise self.error


class FakeMuter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[bool] = []

    async def __call__(self, *, muted: bool) -> None:
        self.calls.append(muted)
        if self.error is not None:
            raise self.error


class EngineScript(TypedDict, total=False):
    tail_seconds: float
    stream_audio: PlayStream
    journal: RecordingJournal
    faults: FaultReporter
    cleanup_timeout_seconds: float


def build_engine(
    synth: SpeechSynthesizer,
    player: PlayStream,
    muter: SetMuted,
    events: list[PlaybackEvent],
    **script: Unpack[EngineScript],
) -> PlaybackEngine:
    return PlaybackEngine(
        synth,
        PlaybackBackend(muter, script.get("stream_audio", player)),
        on_event=events.append,
        timing=PlaybackTiming(
            tail_seconds=script.get("tail_seconds", 0.5),
            cleanup_timeout_seconds=script.get("cleanup_timeout_seconds", 5.0),
        ),
        diagnostics=Diagnostics(faults=script.get("faults"), journal=script.get("journal")),
    )
