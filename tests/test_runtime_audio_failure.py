from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from functools import partial
from types import SimpleNamespace
from typing import Any, override

import pytest
from playwright.async_api import Error as PlaywrightError

import fakeyou.orchestration.runtime as runtime
from fakeyou.audio.audio import MeetingAudioError
from fakeyou.meet.meet import LeaveResult
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from tests._audio_fakes import FakeAudio
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import audio_session


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return audio_session(monkeypatch)


def _faulty_reporter() -> FaultReporter:
    reporter = FaultReporter(journal=Journal("meeting"))
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    return reporter


@pytest.mark.parametrize(
    ("message", "detail"),
    [
        ("dial https://leak.example/x?k=sekret failed", "dial <url> failed"),
        ("", "MeetingAudioError"),
    ],
    ids=["masked", "type-name"],
)
async def test_audio_failure_detail_masks_secrets_and_falls_back_to_type_name(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    message: str,
    detail: str,
) -> None:
    class FailedAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            raise MeetingAudioError(message)

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=partial(open_audio, FailedAudio(session.events)), **session.kwargs
        ),
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == detail


async def test_finish_masks_secrets_in_audio_failure_detail(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
) -> None:
    class LeakyAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            raise MeetingAudioError(
                "capture stopped https://leak.example/token?k=sekret sk-abcdefghij1234567890"
            )

    patched = partial(open_audio, LeakyAudio(session.events))
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=patched, faults=FaultReporter(journal=journal), **session.kwargs
        ),
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == "capture stopped <url> <redacted>"


async def test_finish_merges_fault_suffix_on_audio_failure(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:

    reporter = _faulty_reporter()

    class CaptureStoppedAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            raise MeetingAudioError("capture stopped")

    open_audio = partial(open_audio, CaptureStoppedAudio(session.events))

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, faults=reporter, **session.kwargs),
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == "capture stopped [stt_degraded: timeoutx2]"
    assert result.faults == reporter.report()
    assert isinstance(reporter, FaultReporter)


async def test_finish_merges_fault_suffix_on_leave_path(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    reporter = _faulty_reporter()

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, faults=reporter, **session.kwargs),
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert result.detail == "left [stt_degraded: timeoutx2]"


async def test_empty_faults_leave_detail_byte_identical(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
) -> None:

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=open_audio,
            faults=FaultReporter(journal=journal),
            **session.kwargs,
        ),
    )

    assert result.detail == "left"


async def test_broken_reporter_never_changes_exit_path(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
) -> None:

    class _BrokenReporter(FaultReporter):
        @override
        def summaries(self) -> tuple[Any, ...]:
            raise RuntimeError("reporter exploded")

    class CaptureStoppedAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            raise MeetingAudioError("capture stopped")

    open_audio = partial(open_audio, CaptureStoppedAudio(session.events))

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=open_audio,
            faults=_BrokenReporter(journal=journal),
            **session.kwargs,
        ),
    )

    assert result.terminal_reason == "audio_failure"
    assert result.detail == "capture stopped"
    assert result.faults is None


@pytest.mark.parametrize("leave_raises", [False, True])
async def test_audio_failure_remains_primary_when_leave_also_fails(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leave_raises: bool,
) -> None:
    class CaptureStoppedAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            raise MeetingAudioError("capture stopped")

    open_audio = partial(open_audio, CaptureStoppedAudio(session.events))

    async def broken_leave(*args: Any) -> LeaveResult:
        del args
        if leave_raises:
            raise PlaywrightError("leave failed")
        return LeaveResult("failed", "leave failed")

    monkeypatch.setattr(runtime, "leave_meeting", broken_leave)
    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )
    assert result.terminal_reason == "audio_failure"
    assert result.detail == "capture stopped"
