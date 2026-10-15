import asyncio
from pathlib import Path
from typing import override

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.errors import ErrorCategory
from pydantic import SecretStr

import fakeyou.audio.audio as audio
from fakeyou.audio.audio import (
    CaptureObserver,
    MeetingAudioError,
    MeetingAudioSession,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.attribution_probe import AttributionProbe
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.journal import Journal, JournalFile
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakePlayback, FakeTransport, build_pipeline
from tests._persistence_fakes import RecordingJournal


class _FailingProbe(AttributionProbe):
    @override
    def observe_vad(self, *, active: bool) -> None:
        raise OSError("probe gone")


async def test_pipeline_error_chains_frame_detail() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()), playback=FakePlayback()
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is not None
        assert "secret-provider-key" not in str(caught.value.__cause__)


async def test_runner_failure_chains_original_exception() -> None:
    async def boom() -> None:
        raise ValueError("root-boom")

    task = asyncio.create_task(boom())
    session = MeetingAudioSession(build_pipeline(FakePlayback(), task))
    with pytest.raises(MeetingAudioError) as caught:
        await asyncio.wait_for(session.wait_failed(), timeout=2)
    assert isinstance(caught.value.__cause__, ValueError)
    assert "root-boom" in str(caught.value.__cause__)


async def test_terminate_failure_does_not_mask_initialization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _BrokenTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.backend = _RaisingPyAudio()

        @override
        def input(self) -> FrameProcessor:
            raise ValueError("construction-error")

    monkeypatch.setattr(audio, "native_transport", _BrokenTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    with pytest.raises(MeetingAudioError, match="initialization failed"):
        async with open_meeting_audio(
            callbacks=StreamCallbacks(on_transcript),
            components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
            policy=StreamPolicy(
                speech=SpeechPipelineSettings(
                    stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                    tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
                )
            ),
        ):
            pass


async def test_nonpermanent_stt_error_keeps_session_alive() -> None:
    engine = FakePlayback()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.UNKNOWN),
            playback=engine,
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("hello")
        assert engine.spoken == ["hello"]


async def test_permanent_stt_error_fails_session() -> None:
    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(
                error="provider hiccup", finalized=(), category=ErrorCategory.AUTHENTICATION
            ),
            playback=FakePlayback(),
        ),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_terminate_failure_does_not_fail_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RaisingPyAudio:
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    class _NativeTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.backend = _RaisingPyAudio()

    monkeypatch.setattr(audio, "native_transport", _NativeTransport)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ):
        pass


async def test_permanent_error_records_before_kill_with_typed_cause(
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.faults import FaultReporter
    from fakeyou.observability.provider_faults import ProviderFault

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(
                error="provider hiccup", finalized=(), category=ErrorCategory.AUTHENTICATION
            ),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert reporter.total() >= 1
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.source == "stt"
        assert cause.kind == "unauthorized"
        assert cause.retryable is False


async def test_nonpermanent_error_records_without_killing(
    journal: RecordingJournal,
) -> None:
    from fakeyou.observability.faults import FaultReporter

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=(), category=ErrorCategory.CONNECTIVITY),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        assert reporter.total() >= 1
        report = reporter.report()
        assert report is not None
        assert "unavailable" in {entry["kind"] for entry in report["faults"]["kinds"]}


async def test_uncategorized_error_falls_back_to_exception_classification(
    journal: RecordingJournal,
) -> None:
    import httpx2

    from fakeyou.observability.faults import FaultReporter
    from fakeyou.observability.provider_faults import ProviderFault

    reporter = FaultReporter(journal=journal)

    async def on_transcript(frame: TranscriptionFrame) -> None:
        pass

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(
            stt=FakeSTT(
                error="provider blew up", finalized=(), exception=httpx2.ConnectError("socket gone")
            ),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.kind == "unavailable"
        assert cause.retryable is True


async def test_capture_observer_journals_first_probe_vad_failure_only(
    journal: RecordingJournal, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    probe = _FailingProbe(tmp_path / "probe.jsonl")
    observer = CaptureObserver(CaptureActivity(), journal=journal, probe=probe)

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(observer, "push_frame", sink)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    rows = journal.events("vad_observe_failed")
    assert len(rows) == 1
    assert rows[0]["error_type"] == "OSError"


async def test_callback_fault_should_use_audio_journal_without_reporter(tmp_path: Path) -> None:
    from fakeyou.observability.report import read_journal

    path = tmp_path / "events.jsonl"
    journal = Journal("meeting-7", file=JournalFile(path), echo=False)
    failed = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        del frame
        failed.set()
        raise ValueError("private callback data")

    async with open_meeting_audio(
        transport=FakeTransport(),
        callbacks=StreamCallbacks(on_transcript),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        diagnostics=Diagnostics(journal=journal),
        policy=StreamPolicy(
            speech=SpeechPipelineSettings(
                stt_settings=STTSettings(api_key=SecretStr("muse-secret")),
                tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
            )
        ),
    ) as session:
        await session.worker.queue_frame(InputAudioRawFrame(b"\0\0" * 160, 16_000, 1))
        await asyncio.wait_for(failed.wait(), timeout=2)
        session.raise_if_failed()
    journal.close()

    callbacks = [
        row
        for row in read_journal(path)
        if row["event"] == "fault_recorded" and row["kind"] == "callback_failed"
    ]
    assert len(callbacks) == 1
    assert callbacks[0]["meeting_id"] == "meeting-7"
    assert callbacks[0]["at"]
    assert "private callback data" not in path.read_text()


async def test_missing_muse_key_is_reported_as_configuration_not_audio_failure() -> None:
    async def ignore_transcript(frame: TranscriptionFrame) -> None:
        del frame

    with pytest.raises(FakeYouError, match="FAKEYOU_STT__API_KEY") as caught:
        async with open_meeting_audio(
            transport=FakeTransport(),
            callbacks=StreamCallbacks(ignore_transcript),
            components=StreamComponents(stt=None, playback=FakePlayback()),
            policy=StreamPolicy(
                speech=SpeechPipelineSettings(
                    stt_settings=STTSettings(api_key=None),
                    tts_settings=OpenAITTS(api_key=SecretStr("openai-secret")),
                )
            ),
        ):
            pass

    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


async def test_vad_probe_bug_propagates_from_process_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame

    journal = RecordingJournal("meeting-probe")
    probe = _FailingProbe(tmp_path / "probe.jsonl")
    observer = CaptureObserver(CaptureActivity(), journal=journal, probe=probe)

    def boom(*, active: bool) -> None:
        del active
        raise ValueError("bug")

    async def sink(frame: Frame, direction: FrameDirection) -> None:
        del direction
        del frame
        return None

    monkeypatch.setattr(probe, "observe_vad", boom)
    monkeypatch.setattr(observer, "push_frame", sink)
    with pytest.raises(ValueError, match="bug"):
        await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
