import asyncio
from typing import override

import pytest
from pydantic import SecretStr

from fakeyou.audio.audio import (
    MeetingAudioError,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.capture import CaptureError
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.transcription.contract import SpeechSinks
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


class _CrashingSTT:
    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def send_audio(self, pcm: bytes) -> None:
        del pcm
        raise RuntimeError("stt crashed")


def _crashing(sinks: SpeechSinks) -> _CrashingSTT:
    del sinks
    return _CrashingSTT()


async def test_session_transcribes_final_only_and_speaks_explicitly() -> None:
    engine = FakePlayback()
    heard: list[str] = []
    received = asyncio.Event()
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        heard.append(frame.text)
        received.set()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await asyncio.wait_for(received.wait(), timeout=2)
        assert heard == ["heard"]
        assert engine.spoken == []
        assert session.is_speaking is False
        await session.speak("reply")
        assert engine.spoken == ["reply"]
        await session.stop_playback()
        assert engine.stops == 1
        await session.stop()
        await session.stop()
    assert engine.closed


async def test_session_threads_sentence_index_to_playback_engine() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=_policy(),
    ) as session:
        await session.speak("First sentence.", sentence_index=0)
        await session.speak("Second sentence.", sentence_index=1)
        await session.speak("Legacy reply.")
        assert engine.spoken == ["First sentence.", "Second sentence.", "Legacy reply."]
        assert engine.indices == [0, 1, None]
        await session.stop_playback()
        assert engine.stops == 1
        await session.stop()
        await session.stop()
    assert engine.closed


async def test_upstream_error_unblocks_failure_waiter_without_secret() -> None:
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()), playback=FakePlayback()
        ),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        with pytest.raises(MeetingAudioError):
            session.raise_if_failed()


async def test_unexpected_graph_exit_is_reported() -> None:
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=_crashing, playback=FakePlayback()),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError, match="stopped unexpectedly"):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_callback_failure_degrades_without_killing_session(
    journal: RecordingJournal,
) -> None:
    from fakeyou.audio.deaf import CaptureActivity
    from fakeyou.observability.faults import FaultReporter

    activity = CaptureActivity()
    reporter = FaultReporter(journal=journal)
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        del frame
        raise ValueError("secret-callback-data")

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(faults=reporter), playback=FakePlayback(), activity=activity
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("still alive")
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered >= 1
        report = reporter.report()
        assert report is not None
        kinds = {entry["kind"]: entry["count"] for entry in report["faults"]["kinds"]}
        assert kinds.get("callback_failed", 0) >= 1


async def _await_provider_failure(capture: FakeCapture) -> None:
    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="startup-secret", finalized=()),
            playback=FakePlayback(),
        ),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await session.wait_failed()


async def test_provider_error_on_first_audio_fails_fast_without_secret() -> None:
    async with asyncio.timeout(2):
        with pytest.raises(MeetingAudioError) as caught:
            await _await_provider_failure(FakeCapture())
    assert "startup-secret" not in str(caught.value)


async def test_capture_is_terminated_when_startup_fails() -> None:
    class _BrokenCapture(FakeCapture):
        @override
        async def start(self) -> None:
            raise CaptureError("secret-start-error")

    capture = _BrokenCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    with pytest.raises(MeetingAudioError) as caught:
        async with open_meeting_audio(
            source=capture,
            callbacks=StreamCallbacks(on_transcript),
            components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
            policy=_policy(),
        ):
            pass
    assert capture.terminated == 1
    assert isinstance(caught.value.__cause__, CaptureError)
    assert "secret" not in str(caught.value)


async def test_interim_transcripts_route_to_on_interim_and_activity() -> None:
    from fakeyou.audio.deaf import CaptureActivity

    activity = CaptureActivity()
    interims: list[str] = []
    finals: list[str] = []
    interim_seen = asyncio.Event()
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        finals.append(frame.text)

    async def on_interim(frame: TranscriptUpdate) -> None:
        interims.append(frame.text)
        interim_seen.set()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript, on_interim=on_interim),
        components=StreamComponents(
            stt=FakeSTT("partial", finalized=(False,)), playback=FakePlayback(), activity=activity
        ),
        policy=_policy(),
    ):
        capture.push(b"\0\0" * 160)
        await asyncio.wait_for(interim_seen.wait(), timeout=2)
        await asyncio.sleep(0.2)
        assert interims == ["partial"]
        assert finals == []
        snapshot = activity.snapshot()
        assert snapshot.frames_delivered >= 1
        assert snapshot.finals_delivered == 0
        assert snapshot.interim_count >= 1


async def test_session_marks_activity_unavailable_on_provider_failure() -> None:
    from fakeyou.audio.deaf import CaptureActivity

    activity = CaptureActivity()
    capture = FakeCapture()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()),
            playback=FakePlayback(),
            activity=activity,
        ),
        policy=_policy(),
    ) as session:
        assert activity.snapshot().available is True
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert activity.snapshot().available is False


async def test_session_threads_transcript_id_to_playback_engine() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptUpdate) -> None:
        pass

    async with open_meeting_audio(
        source=FakeCapture(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=engine),
        policy=_policy(),
    ) as session:
        await session.speak("Hello.", sentence_index=0, transcript_id="t9")
        await session.speak("Legacy reply.")
        assert engine.spoken == ["Hello.", "Legacy reply."]
        assert engine.transcripts == ["t9", None]
        await session.stop()
