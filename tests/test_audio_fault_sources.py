from typing import override

import pytest
from pydantic import SecretStr

from fakeyou.audio.audio import (
    MeetingAudioError,
    SessionRuntime,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.deaf import CaptureActivity, DeafTiming
from fakeyou.audio.playback import PlaybackError
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakeCapture, FakePlayback
from tests._persistence_fakes import RecordingJournal


def _policy() -> StreamPolicy:
    return StreamPolicy(
        speech=SpeechPipelineSettings(
            stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
            tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
        )
    )


async def _ignore(update: TranscriptUpdate) -> None:
    del update


class _FailingPlayback(FakePlayback):
    @override
    async def speak(self, text: str, **kwargs: object) -> None:
        raise PlaybackError("paplay exited with status 1")


async def test_synthesis_failure_is_recorded_as_a_named_tts_fault() -> None:
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(stt=FakeSTT(), playback=_FailingPlayback()),
        diagnostics=Diagnostics(faults=faults, journal=journal),
        policy=_policy(),
    ) as session:
        with pytest.raises(PlaybackError):
            await session.speak("hello")

    rows = faults.summaries()
    assert [(row.source, row.kind) for row in rows] == [("tts", "unknown")]
    assert rows[0].detail is not None


async def test_terminal_deaf_verdict_is_recorded_as_a_named_audio_fault() -> None:
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    clock = [1000.0]
    activity = CaptureActivity(now=lambda: clock[0])

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(),
            playback=FakePlayback(),
            activity=activity,
            runtime=SessionRuntime(now=lambda: clock[0]),
        ),
        diagnostics=Diagnostics(faults=faults, journal=journal),
        policy=StreamPolicy(timing=DeafTiming(deaf_grace_seconds=1.0), speech=_policy().speech),
    ) as session:
        activity.mark_ready()
        activity.observe_presence(2)
        clock[0] += 31
        assert await session.maintenance_tick(is_busy=False) == "no_audio_frames"
        clock[0] += 120
        with pytest.raises(MeetingAudioError, match="persistent no_audio_frames"):
            await session.maintenance_tick(is_busy=False)

    rows = faults.summaries()
    assert [(row.source, row.kind, row.count) for row in rows] == [("audio", "no_audio_frames", 2)]


async def test_healthy_session_records_no_fault() -> None:
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        diagnostics=Diagnostics(faults=faults, journal=journal),
        policy=_policy(),
    ) as session:
        await session.speak("hello")

    assert faults.summaries() == ()
    assert faults.total() == 0
