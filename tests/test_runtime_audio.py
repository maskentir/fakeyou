import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from types import SimpleNamespace
from typing import Any, NoReturn, override

import pytest

import fakeyou.orchestration.runtime as runtime
from fakeyou.audio.audio import MeetingAudioError
from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig
from fakeyou.meet.meet import CallObservation, JoinResult
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from tests._audio_fakes import FakeAudio
from tests._conversation_fakes import FakeConversation
from tests._fakes_common import FakeClock
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import audio_session


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    return audio_session(monkeypatch)


async def test_audio_is_ready_before_join_and_stops_before_leave(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    open_audio = partial(open_audio, FakeAudio(session.events), started=True)

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, initial_speech="Hello", **session.kwargs),
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert session.events == [
        "audio_started",
        "admitted",
        "camera",
        "microphone",
        "speak:Hello",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_denied_guest_stops_the_audio_opened_for_the_lobby(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(*args: Any) -> JoinResult:
        del args
        session.events.append("denied")
        return JoinResult("denied", "denied")

    monkeypatch.setattr(runtime, "request_guest_join", denied)

    open_audio = partial(open_audio, FakeAudio(session.events), started=True)

    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )
    assert result.terminal_reason == "denied_by_host"
    assert session.events == ["audio_started", "denied", "audio_stopped", "browser_closed"]


async def test_owner_stop_during_audio_startup_never_joins(session: SimpleNamespace) -> None:
    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        try:
            session.stop.set()
            await asyncio.Event().wait()
            yield FakeAudio(session.events)
        finally:
            session.events.append("audio_start_cancelled")

    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )
    assert result.terminal_reason == "stopped_by_owner"
    assert session.events == ["audio_start_cancelled", "browser_closed"]


@pytest.mark.parametrize("failure_at", ["startup", "monitor", "microphone"])
async def test_audio_failure_stops_pipeline_and_leaves_with_distinct_reason(
    session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure_at: str
) -> None:
    class FailedAudio(FakeAudio):
        @override
        def raise_if_failed(self) -> None:
            if failure_at == "monitor":
                raise MeetingAudioError("provider failed")

    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        if failure_at == "startup":
            raise MeetingAudioError("startup failed")
        audio = FailedAudio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    async def microphone(*args: Any) -> NoReturn:
        del args
        raise RuntimeError("not enabled")

    if failure_at == "microphone":
        monkeypatch.setattr(runtime, "enable_meeting_microphone", microphone)
    result = await runtime.run_meeting_session(
        session.config, runtime.SessionWiring(open_audio=open_audio, **session.kwargs)
    )
    if failure_at == "startup":
        assert result.terminal_reason == "infra_failure"
        assert "admitted" not in session.events
        assert session.events[-1] == "browser_closed"
        return
    assert result.terminal_reason == "audio_failure"
    assert session.events[-2:] == ["leave", "browser_closed"]
    assert session.events.index("audio_stopped") < session.events.index("leave")


async def test_camera_failure_is_recorded_without_failing_session(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def camera(*args: Any) -> NoReturn:
        del args
        raise RuntimeError("no toggle")

    monkeypatch.setattr(runtime, "disable_meeting_camera", camera)
    reporter = FaultReporter(journal=journal)

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, faults=reporter, **session.kwargs),
    )
    assert result.terminal_reason != "audio_failure"
    assert "leave" in session.events
    assert any(
        (entry.source, entry.operation) == ("browser", "camera") for entry in reporter.summaries()
    )


async def test_known_error_dialogs_are_dismissed_before_the_microphone_is_enabled(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dismiss(*args: Any) -> tuple[str, ...]:
        del args
        return ("Camera not found",)

    monkeypatch.setattr(runtime, "dismiss_known_errors", dismiss)

    await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, journal=journal, **session.kwargs),
    )

    [dismissed] = journal.events("modal_dismissed")
    assert dismissed["detail"] == "Camera not found"
    names = [row["event"] for row in journal.events()]
    assert names.index("modal_dismissed") < names.index("microphone_enabled")


async def test_a_failed_dialog_dismissal_is_recorded_without_failing_session(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dismiss(*args: Any) -> NoReturn:
        del args
        raise TimeoutError

    monkeypatch.setattr(runtime, "dismiss_known_errors", dismiss)
    reporter = FaultReporter(journal=journal)

    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, faults=reporter, **session.kwargs),
    )

    assert result.terminal_reason != "audio_failure"
    assert journal.events("microphone_enabled")
    assert any(
        (entry.source, entry.operation) == ("browser", "dismiss_modal")
        for entry in reporter.summaries()
    )


async def test_session_duration_counts_from_admission(
    session: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    admitted_at = []

    async def observe(*args: Any) -> CallObservation:
        del args
        return CallObservation("in_call", "in call")

    async def join(*args: Any) -> JoinResult:
        del args
        await clock.sleep(30.0)
        admitted_at.append(clock.now)
        return JoinResult("admitted", "admitted")

    monkeypatch.setattr(runtime, "observe_call_state", observe)
    monkeypatch.setattr(runtime, "request_guest_join", join)

    @asynccontextmanager
    async def open_audio() -> AsyncIterator[FakeAudio]:
        await clock.sleep(50.0)
        audio = FakeAudio(session.events)
        try:
            yield audio
        finally:
            await audio.stop()

    config = SessionConfig(
        SessionId("owner"),
        SessionId("twin"),
        SessionId("meeting"),
        "https://meet.google.com/abc-defg-hij",
        "Twin",
        session_duration_seconds=100.0,
        monitor_interval_seconds=10.0,
    )
    result = await runtime.run_meeting_session(
        config,
        runtime.SessionWiring(
            open_audio=open_audio,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            **session.kwargs,
        ),
    )
    assert result.terminal_reason == "duration_limit_reached"
    assert clock.sleeps[:2] == [50.0, 30.0]
    assert clock.now - admitted_at[0] == 100.0


async def test_conversation_binds_after_audio_and_closes_before_leave(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    open_audio = partial(open_audio, FakeAudio(session.events), started=True)

    conversation = FakeConversation(session.events)
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, conversation=conversation, **session.kwargs),
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert conversation.bound is not None
    assert session.events == [
        "audio_started",
        "admitted",
        "camera",
        "microphone",
        "conversation_bound",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_conversation_failure_keeps_listening_until_owner_stop(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    open_audio = partial(open_audio, FakeAudio(session.events), started=True)

    conversation = FakeConversation(session.events, fail=True)
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, conversation=conversation, **session.kwargs),
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert "left" in result.detail
    assert session.events == [
        "audio_started",
        "admitted",
        "camera",
        "microphone",
        "conversation_bound",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_owner_stop_interrupts_blocked_initial_speech(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    class BlockingAudio(FakeAudio):
        @override
        async def speak(
            self,
            text: str,
            *,
            sentence_index: int | None = None,
            transcript_id: str | None = None,
            language: str | None = None,
        ) -> None:
            del language
            self.events.append("greeting_started")
            session.stop.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.events.append("greeting_cancelled")

    open_audio = partial(open_audio, BlockingAudio(session.events))

    conversation = FakeConversation(session.events)
    result = await asyncio.wait_for(
        runtime.run_meeting_session(
            session.config,
            runtime.SessionWiring(
                open_audio=open_audio,
                initial_speech="Hello",
                conversation=conversation,
                **session.kwargs,
            ),
        ),
        timeout=1,
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert conversation.bound is None
    assert session.events == [
        "admitted",
        "camera",
        "microphone",
        "greeting_started",
        "greeting_cancelled",
        "conversation_closed",
        "audio_stopped",
        "leave",
        "browser_closed",
    ]


async def test_capture_drain_starts_before_initial_speech(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]], session: SimpleNamespace
) -> None:
    import json
    from dataclasses import replace

    journal = Journal(
        "meeting",
        emit=lambda line: session.events.append("journal:" + json.loads(line)["event"]),
        echo=False,
    )
    open_audio = partial(open_audio, FakeAudio(session.events), started=True)
    config = replace(session.config, per_stream_enabled=True)

    result = await runtime.run_meeting_session(
        config,
        runtime.SessionWiring(
            open_audio=open_audio, initial_speech="Hello", journal=journal, **session.kwargs
        ),
    )
    journal.close()

    assert result.terminal_reason == "stopped_by_owner"
    assert session.events.index("journal:stream_capture_started") < session.events.index(
        "speak:Hello"
    )


async def test_conversation_requires_audio(session: SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="conversation requires meeting audio"):
        await runtime.run_meeting_session(
            session.config,
            runtime.SessionWiring(conversation=FakeConversation(session.events), **session.kwargs),
        )


async def test_conversation_never_binds_and_closes_after_denial(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def denied(*args: Any) -> JoinResult:
        del args
        return JoinResult("denied", "denied")

    monkeypatch.setattr(runtime, "request_guest_join", denied)

    conversation = FakeConversation(session.events)
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, conversation=conversation, **session.kwargs),
    )

    assert result.terminal_reason == "denied_by_host"
    assert conversation.bound is None
    assert session.events == ["conversation_closed", "audio_stopped", "browser_closed"]


async def test_conversation_closes_before_audio_on_unexpected_monitor_error(
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_observation(*args: Any) -> NoReturn:
        del args
        raise RuntimeError("browser observation failed")

    monkeypatch.setattr(runtime, "observe_call_state", broken_observation)

    conversation = FakeConversation(session.events)
    result = await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(open_audio=open_audio, conversation=conversation, **session.kwargs),
    )

    assert result.terminal_reason == "browser_failure"
    assert session.events.index("conversation_closed") < session.events.index("audio_stopped")


async def test_startup_milestones_are_journaled_and_capture_clock_restarts_at_admission(
    journal: RecordingJournal,
    open_audio: Callable[..., AbstractAsyncContextManager[FakeAudio]],
    session: SimpleNamespace,
) -> None:
    import json

    lines = journal.lines

    class AdmissionAwareAudio(FakeAudio):
        def mark_admitted(self) -> None:
            self.events.append("capture_clock_restarted")

    open_audio = partial(open_audio, AdmissionAwareAudio(session.events))

    conversation = FakeConversation(session.events)
    await runtime.run_meeting_session(
        session.config,
        runtime.SessionWiring(
            open_audio=open_audio,
            conversation=conversation,
            journal=journal,
            **session.kwargs,
        ),
    )

    milestones = [
        json.loads(line)["event"] for line in lines if json.loads(line).get("source") == "runtime"
    ]
    assert milestones == [
        "audio_opened",
        "join_requested",
        "join_finished",
        "join_attempt_finished",
        "microphone_enabled",
        "conversation_bound",
    ]
    assert session.events.index("admitted") < session.events.index("capture_clock_restarted")
    assert session.events.index("capture_clock_restarted") < session.events.index("microphone")
