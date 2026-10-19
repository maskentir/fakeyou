import asyncio
from typing import Any, cast, override

import pytest
from pydantic import SecretStr

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    SessionCapture,
    SessionRun,
    SessionRuntime,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.graph import AudioGraph
from fakeyou.audio.playback import PlaybackError, PlaybackPort
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakeCapture, FakePlayback, wait_gate


class _BlockingSTT(FakeSTT):
    def __init__(self, running: asyncio.Event) -> None:
        super().__init__()
        self._running = running

    @override
    async def start(self) -> None:
        self._running.set()
        await asyncio.Event().wait()


async def test_cancellation_during_startup_closes_the_speech_session() -> None:
    running = asyncio.Event()
    stt = _BlockingSTT(running)

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    context = open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=stt, playback=FakePlayback()),
        policy=_policy(),
    )
    entering = asyncio.create_task(context.__aenter__())
    await asyncio.wait_for(running.wait(), timeout=2)
    entering.cancel()
    with pytest.raises(asyncio.CancelledError):
        await entering
    assert stt.closed


def _policy() -> StreamPolicy:
    return StreamPolicy(
        speech=SpeechPipelineSettings(
            stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
            tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
        )
    )


def _session(
    engine: PlaybackPort,
    task: asyncio.Task[None],
    *,
    stt: Any = None,
    capture: FakeCapture | None = None,
) -> MeetingAudioSession:
    return MeetingAudioSession(
        SessionRun(
            graph=cast(AudioGraph, object()),
            task=task,
            source=capture or FakeCapture(),
            engine=engine,
        ),
        capture=SessionCapture(stt=stt, activity=CaptureActivity()),
        diagnostics=Diagnostics(),
        runtime=SessionRuntime(),
    )


async def _finished_task() -> asyncio.Task[None]:
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    return task


class _FailingStopPlayback(FakePlayback):
    @override
    async def stop(self) -> None:
        raise ValueError("bug")


async def test_session_stop_propagates_unexpected_engine_error() -> None:
    session = _session(_FailingStopPlayback(), await _finished_task())

    with pytest.raises(ValueError, match="bug"):
        await session.stop()


class _BuggyCloseSTT:
    async def close(self) -> None:
        raise ValueError("bug")


async def test_session_stop_propagates_unexpected_speech_error() -> None:
    session = _session(FakePlayback(), await _finished_task(), stt=_BuggyCloseSTT())

    with pytest.raises(ValueError, match="bug"):
        await session.stop()


async def test_session_stop_reports_hanging_graph_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(audio, "STOP_DEADLINE_SECONDS", 0.05)
    gate = asyncio.Event()

    async def stubborn() -> None:
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await asyncio.sleep(60.0)

    task = asyncio.create_task(stubborn())
    session = _session(FakePlayback(), task)
    try:
        with pytest.raises(MeetingAudioError, match="shutdown failed"):
            await session.stop()
    finally:
        task.cancel()
        gate.set()
        await asyncio.gather(task, return_exceptions=True)


class _FailingClosePlayback(FakePlayback):
    @override
    async def close(self) -> None:
        raise ValueError("bug")


async def _open_and_close(engine: PlaybackPort) -> None:
    async def on_transcript(frame: TranscriptUpdate) -> None:
        del frame

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=_policy(),
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
    session = _session(_PlaybackErrorStop(), await _finished_task())

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


async def test_stop_runs_all_teardown_when_engine_stop_raises() -> None:
    gate = asyncio.Event()
    task = asyncio.create_task(wait_gate(gate))
    capture = FakeCapture()
    stt = FakeSTT()
    session = _session(_FailingStopEngine(), task, stt=stt, capture=capture)

    with pytest.raises(ValueError, match="bug"):
        await session.stop()

    assert task.cancelled()
    assert capture.terminated == 1
    assert stt.closed is True
