import asyncio
import time
from contextlib import suppress
from typing import Any, cast, override

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.pipeline.worker import PipelineWorker
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner
from pydantic import SecretStr

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionPipeline,
    SessionRuntime,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.playback import PlaybackError, PlaybackPort
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakePlayback, FakeTransport, wait_gate


async def test_cancellation_during_startup_stops_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    running = asyncio.Event()
    released = asyncio.Event()
    cancelled = False

    class _Runner:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def add_workers(self, worker: BaseWorker) -> None:
            pass

        async def run(self) -> None:
            running.set()
            await released.wait()

        async def cancel(self, reason: str) -> None:
            del reason
            nonlocal cancelled
            cancelled = True
            released.set()

    monkeypatch.setattr(audio, "WorkerRunner", _Runner)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    context = open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    )
    entering = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(running.wait(), timeout=2)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert cancelled


async def _completed_runner_task() -> asyncio.Task[None]:
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    return task


async def _direct_session_async(engine: Any, runner: Any) -> MeetingAudioSession:
    pipeline = SessionPipeline(
        cast(PipelineWorker, object()),
        cast(WorkerRunner, runner),
        await _completed_runner_task(),
        engine,
    )
    return MeetingAudioSession(
        pipeline,
        capture=SessionCapture(stt=None, activity=CaptureActivity()),
        diagnostics=Diagnostics(),
    )


class _FailingStopPlayback(FakePlayback):
    @override
    async def stop(self) -> None:
        raise ValueError("bug")


async def test_session_stop_propagates_unexpected_engine_error() -> None:
    session = await _direct_session_async(_FailingStopPlayback(), _Runner())

    with pytest.raises(ValueError, match="bug"):
        await session.stop()


class _BuggyRunner:
    async def cancel(self, reason: str | None = None) -> None:
        del reason
        raise ValueError("bug")


async def test_session_stop_propagates_unexpected_runner_error() -> None:
    session = await _direct_session_async(FakePlayback(), _BuggyRunner())

    with pytest.raises(ValueError, match="bug"):
        await session.stop()


class _HangingRunner:
    async def cancel(self, reason: str | None = None) -> None:
        del reason
        await asyncio.sleep(60.0)


async def test_session_stop_reports_hanging_runner_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio, "STOP_DEADLINE_SECONDS", 0.05)
    session = await _direct_session_async(FakePlayback(), _HangingRunner())

    with pytest.raises(MeetingAudioError, match="shutdown failed"):
        await session.stop()


class _FailingClosePlayback(FakePlayback):
    @override
    async def close(self) -> None:
        raise ValueError("bug")


async def _open_and_close(engine: PlaybackPort) -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        del frame

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ):
        pass


async def test_audio_cleanup_propagates_engine_close_bug() -> None:
    with pytest.raises(ValueError, match="bug"):
        await _open_and_close(_FailingClosePlayback())


class _PlaybackErrorStop(FakePlayback):
    @override
    async def stop(self) -> None:
        raise PlaybackError("paplay gone")


async def test_session_stop_collects_engine_playback_error() -> None:
    session = await _direct_session_async(_PlaybackErrorStop(), _Runner())

    with pytest.raises(MeetingAudioError, match="shutdown failed"):
        await session.stop()


async def test_audio_cleanup_collects_session_shutdown_error() -> None:
    with pytest.raises(MeetingAudioError, match="cleanup failed") as caught:
        await _open_and_close(_PlaybackErrorStop())

    assert isinstance(caught.value.__cause__, MeetingAudioError)
    assert str(caught.value.__cause__) == "meeting audio shutdown failed"


class _PlaybackErrorClose(FakePlayback):
    @override
    async def close(self) -> None:
        raise PlaybackError("paplay gone")


async def test_audio_cleanup_collects_engine_close_playback_error() -> None:
    with pytest.raises(MeetingAudioError, match="cleanup failed") as caught:
        await _open_and_close(_PlaybackErrorClose())

    assert isinstance(caught.value.__cause__, PlaybackError)


class _FailingStopEngine(FakePlayback):
    @override
    async def stop(self) -> None:
        raise ValueError("bug")


async def test_audio_cleanup_propagates_session_stop_bug_and_still_closes_engine() -> None:
    engine = _FailingStopEngine()

    with pytest.raises(ValueError, match="bug"):
        await _open_and_close(engine)

    assert engine.closed is True


class _Runner:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.cancelled: list[str] = []

    async def cancel(self, reason: str) -> None:
        self.cancelled.append(reason)
        if self.error is not None:
            raise self.error


def _stopping_session(
    engine: PlaybackPort, runner: _Runner, runner_task: asyncio.Task[None]
) -> MeetingAudioSession:
    pipeline = SessionPipeline(
        cast(PipelineWorker, object()), cast(WorkerRunner, runner), runner_task, engine
    )
    return MeetingAudioSession(
        pipeline,
        capture=SessionCapture(stt=FakeSTT(), activity=CaptureActivity()),
        diagnostics=Diagnostics(),
        runtime=SessionRuntime(now=time.monotonic),
    )


async def test_stop_runs_runner_cancel_when_engine_stop_raises_bug() -> None:
    gate = asyncio.Event()
    gate.set()
    runner_task = asyncio.create_task(wait_gate(gate))
    await asyncio.sleep(0)
    runner = _Runner()
    session = _stopping_session(_FailingStopEngine(), runner, runner_task)

    with pytest.raises(ValueError, match="bug"):
        await session.stop()

    assert runner.cancelled == ["meeting audio stopped"]
    await runner_task


async def test_stop_cancels_runner_task_when_engine_stop_raises_bug() -> None:
    gate = asyncio.Event()
    runner_task = asyncio.create_task(wait_gate(gate))
    runner = _Runner(error=TimeoutError("runner stuck"))
    session = _stopping_session(_FailingStopEngine(), runner, runner_task)
    try:
        with pytest.raises(ValueError, match="bug"):
            await session.stop()

        assert runner_task.cancelling() == 1
    finally:
        gate.set()
        with suppress(asyncio.CancelledError):
            await runner_task
