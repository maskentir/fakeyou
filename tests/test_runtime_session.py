import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn, override

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import OperationalError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.conversation.session import ConversationError, SessionResult
from fakeyou.observability.cleanup import TeardownStack
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.lifecycle import (
    LifecycleThread,
    build_lifecycle_graph,
    initial_lifecycle_state,
    initialize_lifecycle,
)
from fakeyou.orchestration.runtime import (
    SessionWiring,
    drive_session,
    run_meeting_session,
    session_resources,
)
from tests._error_support import message_pattern
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import (
    ASK_SELECTOR,
    DENIED,
    ENDED,
    EXPLICIT_DENIED,
    IN_CALL,
    LEAVE_SELECTOR,
    LOBBY,
    NAME_SELECTOR,
    NO_CAPTCHA,
    REMOVED,
    FakePage,
    FakePageSession,
    meet_page,
)
from tests._persistence_fakes import FakeSaverSession, RecordingJournal
from tests._runtime_support import pin_join_jitter, run_session, session_config


@pytest.fixture(autouse=True)
def instant_join_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_join_jitter(monkeypatch)


class _BlockingTerminalSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_started = asyncio.Event()
        self.release_terminal = asyncio.Event()
        self.terminal_persisted = asyncio.Event()
        self.terminal_cancelled = asyncio.Event()
        self._blocked = False

    def factory(self) -> AbstractAsyncContextManager["_BlockingTerminalSaver"]:
        saver = self

        @asynccontextmanager
        async def _session() -> AsyncIterator[_BlockingTerminalSaver]:
            yield saver

        return _session()

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        values = checkpoint.get("channel_values", {})
        if (
            not self._blocked
            and values.get("phase") == "ended"
            and values.get("terminal_reason") == "stopped_by_owner"
        ):
            self._blocked = True
            self.terminal_started.set()
            try:
                await self.release_terminal.wait()
            except asyncio.CancelledError:
                self.terminal_cancelled.set()
                raise
        result = await super().aput(config, checkpoint, metadata, new_versions)
        if self._blocked and values.get("phase") == "ended":
            self.terminal_persisted.set()
        return result


async def test_run_meeting_session_aborts_join_when_stop_requested() -> None:
    browser = FakePageSession(meet_page([]))
    saver = FakeSaverSession()
    stop = asyncio.Event()
    stop.set()

    result = await run_session(session_config(headless=True), browser, saver, FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "stopped_by_owner"
    assert browser.opened == 0
    assert browser.closed == 0


async def test_run_meeting_session_stops_and_leaves_after_admission() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, ENDED],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "stopped_by_owner"


async def test_run_meeting_session_enforces_duration_limit() -> None:
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED]))

    result = await run_session(
        session_config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        FakeSaverSession(),
        FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"


async def test_run_meeting_session_logs_slow_monitor_poll(
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = FakeClock()

    def advance_on_monitor_poll(reads: int) -> None:
        if reads == 2:
            clock.now += 2.5

    page = meet_page([IN_CALL, IN_CALL, ENDED], on_content=advance_on_monitor_poll)
    browser = FakePageSession(page)

    result = await run_session(session_config(), browser, FakeSaverSession(), clock)

    assert result.terminal_reason == "ended_by_host"
    polls = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if json.loads(line).get("event") == "monitor_polled"
    ]
    assert len(polls) == 1
    assert polls[0]["meeting_id"] == "meeting-1"
    assert polls[0]["duration_ms"] == 2500


async def test_run_meeting_session_skips_monitor_poll_log_when_fast(
    capsys: pytest.CaptureFixture[str],
) -> None:
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, ENDED]))

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock())

    assert result.terminal_reason == "ended_by_host"
    assert "monitor_polled" not in capsys.readouterr().out


async def test_run_meeting_session_recovers_from_transient_lobby() -> None:
    browser = FakePageSession(meet_page([IN_CALL, LOBBY, IN_CALL, REMOVED]))

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "removed_by_host"


async def test_run_meeting_session_enforces_duration_limit_while_disconnected() -> None:
    page = meet_page([IN_CALL, LOBBY, LOBBY, LOBBY])
    browser = FakePageSession(page)

    result = await run_session(
        session_config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        FakeSaverSession(),
        FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"
    assert ASK_SELECTOR in page.clicked
    assert LEAVE_SELECTOR not in page.clicked


async def test_run_meeting_session_maps_leave_failure_to_browser_failure() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 1 else None,
    )
    browser = FakePageSession(page)

    result = await run_session(
        session_config(leave_timeout_seconds=3.0), browser, FakeSaverSession(), FakeClock(), stop
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_maps_observation_crash_to_browser_failure() -> None:
    browser = FakePageSession(meet_page([IN_CALL, RuntimeError("boom")]))

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_sanitizes_browser_failure_detail() -> None:
    events: list[str] = []
    journal = Journal("meeting-1", emit=events.append, echo=False)
    clock = FakeClock()

    def failing_open(*args: object, **kwargs: object) -> NoReturn:
        del args
        del kwargs
        raise RuntimeError("boom secret-token")

    result = await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=failing_open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"
    assert result.detail == "browser session failed (RuntimeError)"
    assert "secret-token" not in result.detail
    rows = [json.loads(line) for line in events]
    details = [row for row in rows if row["event"] == "browser_error_recorded"]
    assert len(details) == 1
    assert details[0]["error"] == "boom secret-token"
    assert details[0]["error_type"] == "RuntimeError"


async def test_run_meeting_session_records_bounded_traceback_on_browser_failure() -> None:
    events: list[str] = []
    journal = Journal("meeting-1", emit=events.append, echo=False)
    clock = FakeClock()

    def failing_open(*args: object, **kwargs: object) -> NoReturn:
        del args
        del kwargs
        raise RuntimeError("boom")

    result = await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=failing_open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.phase == "ended"
    rows = [json.loads(line) for line in events]
    [detail] = [row for row in rows if row["event"] == "browser_error_recorded"]
    assert "RuntimeError" in detail["traceback"]
    assert "failing_open" in detail["traceback"]
    assert len(detail["traceback"]) <= 2000


async def test_run_meeting_session_records_bounded_traceback_on_conversation_failure() -> None:
    events: list[str] = []
    journal = Journal("meeting-1", emit=events.append, echo=False)
    clock = FakeClock()

    def failing_open(*args: object, **kwargs: object) -> NoReturn:
        del args
        del kwargs
        raise ConversationError("talk broke")

    result = await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=failing_open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.phase == "failed"
    assert result.terminal_reason == "conversation_failure"
    assert result.detail == "conversation failed (ConversationError)"
    rows = [json.loads(line) for line in events]
    [detail] = [row for row in rows if row["event"] == "conversation_error_recorded"]
    assert "ConversationError" in detail["traceback"]
    assert "failing_open" in detail["traceback"]
    assert len(detail["traceback"]) <= 2000


async def test_run_meeting_session_reraises_catalog_errors_unmangled() -> None:
    clock = FakeClock()

    def failing_open(*args: object, **kwargs: object) -> NoReturn:
        del args
        del kwargs
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as error:
        await run_meeting_session(
            session_config(),
            SessionWiring(
                open_page=failing_open,
                saver_factory=FakeSaverSession().factory,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            ),
        )

    assert error.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_run_meeting_session_tolerates_single_observation_blip() -> None:
    from playwright.async_api import Error as PlaywrightError

    browser = FakePageSession(meet_page([IN_CALL, PlaywrightError("boom"), IN_CALL, ENDED]))

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "ended_by_host"


async def test_run_meeting_session_returns_ended_result_without_reopening_browser() -> None:
    saver = FakeSaverSession()
    first = FakePageSession(meet_page([DENIED], evaluates=[NO_CAPTCHA]))

    result = await run_session(session_config(), first, saver, FakeClock())

    assert result.terminal_reason == "blocked_by_platform"
    second = FakePageSession(meet_page([LOBBY]))
    again = await run_session(session_config(), second, saver, FakeClock())

    assert again.phase == "ended"
    assert again.terminal_reason == "blocked_by_platform"
    assert second.opened == 0


async def test_run_meeting_session_rejects_resume_of_interrupted_session() -> None:
    saver = FakeSaverSession()
    graph = build_lifecycle_graph(checkpointer=saver.saver)
    await graph.aupdate_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}},
        {**initial_lifecycle_state("owner-1", "twin-1", "meeting-1"), "phase": "lobby"},
        as_node="join",
    )
    browser = FakePageSession(meet_page([LOBBY]))

    with pytest.raises(FakeYouError, match="cannot resume") as caught:
        await run_session(session_config(), browser, saver, FakeClock())

    assert caught.value.code is ErrorCode.MEETING_CANNOT_RESUME_PHASE
    assert browser.opened == 0


async def test_browser_startup_failure_is_persisted_as_terminal() -> None:
    saver = FakeSaverSession()

    @asynccontextmanager
    async def fail_open(**kwargs: Any) -> AsyncIterator[Any]:
        del kwargs
        raise RuntimeError("browser launch failed")
        yield

    result = await run_meeting_session(
        session_config(), SessionWiring(open_page=fail_open, saver_factory=saver.factory)
    )

    assert result.terminal_reason == "browser_failure"
    state = await build_lifecycle_graph(saver.saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert state.values["phase"] == "ended"
    assert state.values["external_effects"] == []


async def test_stop_arriving_with_browser_startup_does_not_request_join() -> None:
    stop = asyncio.Event()
    page = meet_page()
    browser = FakePageSession(page)
    saver = FakeSaverSession()

    @asynccontextmanager
    async def open_then_stop(**kwargs: Any) -> AsyncIterator[FakePage]:
        async with browser.open(**kwargs) as opened:
            stop.set()
            yield opened

    result = await run_meeting_session(
        session_config(),
        SessionWiring(stop=stop, open_page=open_then_stop, saver_factory=saver.factory),
    )

    assert result.terminal_reason == "stopped_by_owner"
    assert page.visited == []
    assert page.clicked == []
    assert browser.closed == 1


async def test_leave_crash_is_persisted_and_browser_closed() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, RuntimeError("browser vanished while leaving")],
        on_content=lambda reads: stop.set() if reads == 1 else None,
    )
    browser = FakePageSession(page)

    result = await run_session(session_config(), browser, FakeSaverSession(), FakeClock(), stop)

    assert result.terminal_reason == "browser_failure"
    assert browser.closed == 1


async def test_session_rejects_checkpoint_owned_by_another_owner() -> None:
    saver = FakeSaverSession()
    first = FakePageSession(meet_page([DENIED], evaluates=[NO_CAPTCHA]))
    await run_session(session_config(), first, saver, FakeClock())
    other = FakePageSession(meet_page())

    with pytest.raises(FakeYouError, match="identity") as caught:
        await run_session(session_config(owner_id="other-owner"), other, saver, FakeClock())

    assert caught.value.code is ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH
    assert other.opened == 0


async def test_stop_cancels_pending_navigation_and_closes_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    navigation_started = asyncio.Event()
    navigation_cancelled = asyncio.Event()
    page = meet_page()

    async def slow_navigation(*args: Any, **kwargs: Any) -> None:
        del args
        del kwargs
        navigation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            navigation_cancelled.set()

    monkeypatch.setattr(page, "goto", slow_navigation)
    browser = FakePageSession(page)
    async with asyncio.TaskGroup() as tasks:
        running = tasks.create_task(
            run_session(session_config(), browser, FakeSaverSession(), FakeClock(), stop)
        )
        await asyncio.wait_for(navigation_started.wait(), timeout=1)
        stop.set()
        result = await asyncio.wait_for(running, timeout=1)

    assert result.terminal_reason == "stopped_by_owner"
    assert navigation_cancelled.is_set()
    assert page.clicked == []
    assert browser.closed == 1


async def test_repeated_cancellation_preserves_terminal_checkpoint() -> None:
    admitted = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, "left"],
        on_content=lambda reads: admitted.set() if reads == 2 else None,
    )
    browser = FakePageSession(page)
    saver = _BlockingTerminalSaver()

    async def paced_sleep(seconds: float) -> None:
        await asyncio.sleep(min(seconds, 0.01))

    running = asyncio.create_task(
        run_meeting_session(
            session_config(monitor_interval_seconds=0.01),
            SessionWiring(
                open_page=browser.open,
                saver_factory=saver.factory,
                sleep=paced_sleep,
                monotonic=time.monotonic,
            ),
        )
    )
    try:
        await asyncio.wait_for(admitted.wait(), timeout=1)
        running.cancel()
        await asyncio.wait_for(saver.terminal_started.wait(), timeout=1)
        assert not running.done()
        running.cancel()
        await asyncio.sleep(0)
        saver.release_terminal.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
        await asyncio.wait_for(saver.terminal_persisted.wait(), timeout=1)
        assert not saver.terminal_cancelled.is_set()
    finally:
        saver.release_terminal.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    state = await build_lifecycle_graph(saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert state.values["phase"] == "ended"
    assert state.values["terminal_reason"] == "stopped_by_owner"
    assert browser.closed == 1


async def test_drive_session_cancel_preserved_when_cancel_record_send_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def cancelled(*args: Any, **kwargs: Any) -> NoReturn:
        del args
        del kwargs
        raise asyncio.CancelledError

    async def failing_send(*args: Any, **kwargs: Any) -> NoReturn:
        del args
        del kwargs
        raise ValueError("poison head")

    monkeypatch.setattr("fakeyou.orchestration.runtime.startup_session", cancelled)
    lifecycle = LifecycleThread(
        build_lifecycle_graph(InMemorySaver()),
        "twin-1:meeting-1",
        initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
    )
    await initialize_lifecycle(lifecycle, resume=False)
    monkeypatch.setattr(LifecycleThread, "send", failing_send)
    journal = Journal("meeting-1", emit=lambda _line: None, echo=False)
    with pytest.raises(asyncio.CancelledError):
        await drive_session(
            session_config(),
            SessionWiring(journal=journal),
            asyncio.Event(),
            TeardownStack(),
            lifecycle,
        )


async def test_cancellation_during_leave_preserves_cancellation_and_terminal_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    leaving = asyncio.Event()
    page = meet_page()
    saver = FakeSaverSession()

    async def content() -> str:
        if not stop.is_set():
            stop.set()
            return IN_CALL
        leaving.set()
        await asyncio.Event().wait()
        raise AssertionError("terminal wait returned without cancellation")

    monkeypatch.setattr(page, "content", content)
    browser = FakePageSession(page)
    running = asyncio.create_task(run_session(session_config(), browser, saver, FakeClock(), stop))
    try:
        await asyncio.wait_for(leaving.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    saved = await build_lifecycle_graph(saver.saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert saved.values["phase"] == "ended"
    assert saved.values["terminal_reason"] == "stopped_by_owner"
    assert page.clicked.count(LEAVE_SELECTOR) == 1
    assert browser.closed == 1


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
@pytest.mark.parametrize("value", ["", "../another-profile", "contains:separator"])
def test_session_rejects_invalid_identity(field: str, value: str) -> None:
    with pytest.raises(ValueError, match="String should match pattern"):
        session_config(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "lobby_timeout_seconds",
        "join_poll_interval_seconds",
        "session_duration_seconds",
        "monitor_interval_seconds",
        "leave_timeout_seconds",
        "deaf_window_seconds",
        "deaf_grace_seconds",
        "preflight_budget_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_session_rejects_invalid_timing(field: str, value: float | int) -> None:
    expected = "finite number" if isinstance(value, float) else "greater than 0"
    with pytest.raises(ValueError, match=expected):
        session_config(**{field: value})


async def test_monitor_poll_should_persist_through_the_supplied_journal(tmp_path: Path) -> None:
    from fakeyou.observability.journal import Journal, JournalFile
    from fakeyou.observability.report import read_journal

    clock = FakeClock()

    def advance_on_monitor_poll(reads: int) -> None:
        if reads == 2:
            clock.now += 2.5

    browser = FakePageSession(
        meet_page([IN_CALL, IN_CALL, ENDED], on_content=advance_on_monitor_poll)
    )
    saver = FakeSaverSession()
    path = tmp_path / "events.jsonl"
    journal = Journal("journal-meeting", file=JournalFile(path), echo=False)
    await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open,
            saver_factory=saver.factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )
    journal.close()

    polls = [row for row in read_journal(path) if row["event"] == "monitor_polled"]
    assert len(polls) == 1
    assert polls[0]["meeting_id"] == "journal-meeting"
    assert polls[0]["duration_ms"] == 2500
    assert polls[0]["at"]


@pytest.mark.parametrize(
    "case",
    [
        ([EXPLICIT_DENIED], [], {}, {}, "denied_by_host", ""),
        ([DENIED], [NO_CAPTCHA], {}, {}, "blocked_by_platform", ""),
        (
            [LOBBY] * 15,
            [],
            {},
            {"lobby_timeout_seconds": 5.0, "join_poll_interval_seconds": 2.0},
            "lobby_timeout_reached",
            "",
        ),
        ([], [], {NAME_SELECTOR: [0]}, {}, "browser_failure", "name field"),
        ([RuntimeError("cdp gone")], [], {}, {}, "browser_failure", ""),
        ([IN_CALL, IN_CALL, REMOVED], [], {}, {}, "removed_by_host", ""),
        ([IN_CALL, IN_CALL, ENDED], [], {}, {}, "ended_by_host", ""),
    ],
    ids=["denied", "blocked", "lobby_timed_out", "missing_name", "join_crash", "removed", "ended"],
)
async def test_run_meeting_session_reports_terminal_outcome(
    case: tuple[list[Any], list[Any], dict[str, Any], dict[str, Any], str, str],
) -> None:
    contents, evaluates, counts, overrides, expected, detail = case
    browser = FakePageSession(meet_page(contents, evaluates=evaluates, counts=counts))
    result = await run_session(
        session_config(**overrides), browser, FakeSaverSession(), FakeClock()
    )
    assert result.phase == "ended"
    assert result.terminal_reason == expected
    assert detail in result.detail
    assert browser.closed == 1


async def test_lobby_timeout_retries_twice_before_terminal(journal: RecordingJournal) -> None:
    browser = FakePageSession(meet_page([LOBBY] * 15))
    clock = FakeClock()
    config = session_config(lobby_timeout_seconds=5.0, join_poll_interval_seconds=2.0)
    result = await run_meeting_session(
        config,
        SessionWiring(
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "lobby_timeout_reached"
    rows = journal.events()
    attempts = [
        (row["attempt"], row["outcome"]) for row in rows if row["event"] == "join_attempt_finished"
    ]
    assert attempts == [(1, "retry"), (2, "retry"), (3, "failed")]
    finished = [row for row in rows if row["event"] == "join_finished"]
    assert len(finished) == 1
    assert finished[0]["attempts"] == 3
    assert [row["event"] for row in rows].count("join_requested") == 1


async def test_run_meeting_session_journals_why_the_join_failed() -> None:
    browser = FakePageSession(meet_page([], counts={NAME_SELECTOR: [0]}))
    journal = RecordingJournal()
    clock = FakeClock()
    await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )
    [finished] = journal.events("join_finished")
    assert finished["status"] == "failed"
    assert "name field" in finished["detail"]


async def test_session_resources_drain_callbacks_without_raising() -> None:
    ran: list[str] = []

    async def fail() -> None:
        ran.append("fail")
        raise RuntimeError("teardown boom")

    async def succeed() -> None:
        ran.append("succeed")

    async with session_resources(None) as resources:
        resources.push_async_callback(fail)
        resources.push_async_callback(succeed)

    assert ran == ["succeed", "fail"]


class _FailingWritesSaver(InMemorySaver):
    def __init__(self, failing_phases: set[str]) -> None:
        super().__init__()
        self.failing = set(failing_phases)
        self.written: list[str] = []

    @override
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        phase = str(checkpoint["channel_values"].get("phase"))
        if phase in self.failing:
            self.failing.discard(phase)
            raise OperationalError("connection reset by peer")
        result = await super().aput(config, checkpoint, metadata, new_versions)
        self.written.append(phase)
        return result


async def _duration_session(
    failing: set[str],
) -> tuple[SessionResult, RecordingJournal, _FailingWritesSaver]:
    clock = FakeClock()
    journal = RecordingJournal("meeting-1")
    saver = _FailingWritesSaver(failing)
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED]))

    @asynccontextmanager
    async def factory() -> AsyncIterator[InMemorySaver]:
        yield saver

    result = await run_meeting_session(
        session_config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        SessionWiring(
            open_page=browser.open,
            saver_factory=factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )
    return result, journal, saver


async def test_checkpoint_failures_while_admitted_and_active_degrade_and_replay_in_order() -> None:
    clean_result, _clean_journal, clean = await _duration_session(set())
    degraded_result, journal, degraded = await _duration_session({"active", "leaving"})

    assert clean_result.phase == "ended"
    assert clean_result.terminal_reason == "duration_limit_reached"
    queued = journal.events("lifecycle_persist_queued")
    assert len(queued) == 2
    assert all(row["error_type"] == "OperationalError" for row in queued)
    assert degraded_result.phase == clean_result.phase
    assert degraded_result.terminal_reason == clean_result.terminal_reason
    assert degraded.written == clean.written
    final_clean = await clean.aget_tuple({"configurable": {"thread_id": "twin-1:meeting-1"}})
    final_degraded = await degraded.aget_tuple({"configurable": {"thread_id": "twin-1:meeting-1"}})
    assert final_clean is not None
    assert final_degraded is not None
    for key in ("phase", "terminal_reason", "external_effects"):
        assert (
            final_degraded.checkpoint["channel_values"][key]
            == (final_clean.checkpoint["channel_values"][key])
        )
