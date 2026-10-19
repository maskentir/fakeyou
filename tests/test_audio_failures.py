import asyncio
from typing import override

import httpx2
import pytest
from pydantic import SecretStr

from fakeyou.audio.audio import (
    MeetingAudioError,
    MeetingAudioSession,
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.config.settings import OpenAITTS, STTSettings
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_fakes import FakeSTT
from tests._audio_support import FakeCapture, FakePlayback, build_run
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


async def test_pipeline_error_chains_provider_detail_without_secret() -> None:
    capture = FakeCapture()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(error="secret-provider-key", finalized=()), playback=FakePlayback()
        ),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is not None
        assert "secret-provider-key" not in str(caught.value.__cause__)


async def test_graph_failure_chains_original_exception() -> None:
    async def boom() -> None:
        raise ValueError("root-boom")

    task = asyncio.create_task(boom())
    session = MeetingAudioSession(build_run(FakePlayback(), task))
    with pytest.raises(MeetingAudioError) as caught:
        await asyncio.wait_for(session.wait_failed(), timeout=2)
    assert isinstance(caught.value.__cause__, ValueError)
    assert "root-boom" in str(caught.value.__cause__)


async def test_terminate_failure_does_not_mask_initialization_error() -> None:
    class _BrokenCapture(FakeCapture):
        @override
        async def start(self) -> None:
            raise ValueError("construction-error")

        @override
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    async with asyncio.timeout(2):
        with pytest.raises(MeetingAudioError, match="initialization failed"):
            async with open_meeting_audio(
                source=_BrokenCapture(),
                callbacks=StreamCallbacks(_ignore),
                components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
                policy=_policy(),
            ):
                pass


async def test_retryable_stt_error_records_and_keeps_session_alive() -> None:
    engine = FakePlayback()
    capture = FakeCapture()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=(), exception=_timeout()),
            playback=engine,
        ),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        await session.speak("hello")
        assert engine.spoken == ["hello"]


async def test_permanent_stt_error_fails_session() -> None:
    capture = FakeCapture()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=()), playback=FakePlayback()
        ),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError):
            await asyncio.wait_for(session.wait_failed(), timeout=2)


async def test_terminate_failure_does_not_fail_clean_shutdown() -> None:
    class _TerminateFailsCapture(FakeCapture):
        @override
        def terminate(self) -> None:
            raise RuntimeError("terminate-boom")

    async with open_meeting_audio(
        source=_TerminateFailsCapture(),
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(stt=FakeSTT(), playback=FakePlayback()),
        policy=_policy(),
    ):
        pass


async def test_permanent_error_records_before_kill_with_typed_cause(
    journal: RecordingJournal,
) -> None:
    reporter = FaultReporter(journal=journal)
    capture = FakeCapture()
    rejected = ProviderFault("stt", "unauthorized")

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=(), exception=rejected),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        with pytest.raises(MeetingAudioError) as caught:
            await asyncio.wait_for(session.wait_failed(), timeout=2)
        assert reporter.total() >= 1
        cause = caught.value.__cause__
        assert isinstance(cause, ProviderFault)
        assert cause.source == "stt"
        assert cause.kind == "unauthorized"
        assert cause.retryable is False


async def test_retryable_error_is_reported_with_its_kind_without_killing() -> None:
    reporter = FaultReporter(journal=RecordingJournal("meeting-1"))
    capture = FakeCapture()
    unavailable = ProviderFault("stt", "unavailable", retryable=True)

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(error="provider hiccup", finalized=(), exception=unavailable),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        assert reporter.total() >= 1
        report = reporter.report()
        assert report is not None
        assert "unavailable" in {entry["kind"] for entry in report["faults"]["kinds"]}


async def test_uncategorized_socket_error_is_classified_as_retryable_unavailable() -> None:
    reporter = FaultReporter(journal=RecordingJournal("meeting-2"))
    capture = FakeCapture()

    async with open_meeting_audio(
        source=capture,
        callbacks=StreamCallbacks(_ignore),
        components=StreamComponents(
            stt=FakeSTT(
                error="provider blew up", finalized=(), exception=httpx2.ConnectError("socket gone")
            ),
            playback=FakePlayback(),
        ),
        diagnostics=Diagnostics(faults=reporter),
        policy=_policy(),
    ) as session:
        capture.push(b"\0\0" * 160)
        await asyncio.sleep(0.5)
        session.raise_if_failed()
        report = reporter.report()
        assert report is not None
        assert "unavailable" in {entry["kind"] for entry in report["faults"]["kinds"]}


def _timeout() -> Exception:
    return TimeoutError("provider timeout")
