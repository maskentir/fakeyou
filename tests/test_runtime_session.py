import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, NoReturn

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.conversation.session import ConversationError, SessionResult
from fakeyou.meet.meet import CallObservation
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.runtime import (
    OBSERVATION_GRACE_SECONDS,
    SessionWiring,
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
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import pin_join_jitter, run_session, session_config


@pytest.fixture(autouse=True)
def instant_join_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_join_jitter(monkeypatch)


async def test_run_meeting_session_aborts_join_when_stop_requested() -> None:
    browser = FakePageSession(meet_page([]))
    stop = asyncio.Event()
    stop.set()

    result = await run_session(session_config(headless=True), browser, FakeClock(), stop)

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

    result = await run_session(session_config(), browser, FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "stopped_by_owner"


async def test_run_meeting_session_enforces_duration_limit() -> None:
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED]))

    result = await run_session(
        session_config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
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

    result = await run_session(session_config(), browser, clock)

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

    result = await run_session(session_config(), browser, FakeClock())

    assert result.terminal_reason == "ended_by_host"
    assert "monitor_polled" not in capsys.readouterr().out


async def test_run_meeting_session_recovers_from_transient_lobby() -> None:
    browser = FakePageSession(meet_page([IN_CALL, LOBBY, IN_CALL, REMOVED]))

    result = await run_session(session_config(), browser, FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "removed_by_host"


async def test_run_meeting_session_enforces_duration_limit_while_disconnected() -> None:
    page = meet_page([IN_CALL, LOBBY, LOBBY, LOBBY])
    browser = FakePageSession(page)

    result = await run_session(
        session_config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"
    assert ASK_SELECTOR in page.clicked
    assert LEAVE_SELECTOR not in page.clicked


async def test_a_failed_leave_keeps_the_owner_stop_as_the_reason() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 1 else None,
    )
    browser = FakePageSession(page)

    result = await run_session(
        session_config(leave_timeout_seconds=3.0), browser, FakeClock(), stop
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "stopped_by_owner"
    assert browser.closed == 1


async def test_run_meeting_session_maps_observation_crash_to_browser_failure() -> None:
    browser = FakePageSession(meet_page([IN_CALL, RuntimeError("boom")]))

    result = await run_session(session_config(), browser, FakeClock())

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
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            ),
        )

    assert error.value.code is ErrorCode.DATABASE_UNREACHABLE


async def test_run_meeting_session_tolerates_single_observation_blip() -> None:
    from playwright.async_api import Error as PlaywrightError

    browser = FakePageSession(meet_page([IN_CALL, PlaywrightError("boom"), IN_CALL, ENDED]))

    result = await run_session(session_config(), browser, FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "ended_by_host"


async def test_browser_startup_failure_ends_the_session() -> None:
    @asynccontextmanager
    async def fail_open(**kwargs: Any) -> AsyncIterator[Any]:
        del kwargs
        raise RuntimeError("browser launch failed")
        yield

    result = await run_meeting_session(session_config(), SessionWiring(open_page=fail_open))

    assert (result.phase, result.terminal_reason) == ("ended", "browser_failure")


async def test_stop_arriving_with_browser_startup_does_not_request_join() -> None:
    stop = asyncio.Event()
    page = meet_page()
    browser = FakePageSession(page)

    @asynccontextmanager
    async def open_then_stop(**kwargs: Any) -> AsyncIterator[FakePage]:
        async with browser.open(**kwargs) as opened:
            stop.set()
            yield opened

    result = await run_meeting_session(
        session_config(),
        SessionWiring(stop=stop, open_page=open_then_stop),
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

    result = await run_session(session_config(), browser, FakeClock(), stop)

    assert result.terminal_reason == "browser_failure"
    assert browser.closed == 1


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
        running = tasks.create_task(run_session(session_config(), browser, FakeClock(), stop))
        await asyncio.wait_for(navigation_started.wait(), timeout=1)
        stop.set()
        result = await asyncio.wait_for(running, timeout=1)

    assert result.terminal_reason == "stopped_by_owner"
    assert navigation_cancelled.is_set()
    assert page.clicked == []
    assert browser.closed == 1


async def test_cancellation_during_leave_propagates_and_closes_the_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    leaving = asyncio.Event()
    page = meet_page()

    async def content() -> str:
        if not stop.is_set():
            stop.set()
            return IN_CALL
        leaving.set()
        await asyncio.Event().wait()
        raise AssertionError("terminal wait returned without cancellation")

    monkeypatch.setattr(page, "content", content)
    browser = FakePageSession(page)
    running = asyncio.create_task(run_session(session_config(), browser, FakeClock(), stop))
    try:
        await asyncio.wait_for(leaving.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

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
    path = tmp_path / "events.jsonl"
    journal = Journal("journal-meeting", file=JournalFile(path), echo=False)
    await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open,
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
    result = await run_session(session_config(**overrides), browser, FakeClock())
    assert result.phase == "ended"
    assert result.terminal_reason == expected
    assert detail in result.detail
    assert browser.closed == 1


async def test_run_meeting_session_journals_why_the_join_failed() -> None:
    browser = FakePageSession(meet_page([], counts={NAME_SELECTOR: [0]}))
    journal = RecordingJournal()
    clock = FakeClock()
    await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open,
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


def _script_observations(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[CallObservation | Exception],
    *,
    then: CallObservation | Exception,
) -> list[int]:
    calls = [0]

    async def observe(*args: Any) -> CallObservation:
        del args
        calls[0] += 1
        outcome = outcomes.pop(0) if outcomes else then
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("fakeyou.orchestration.runtime.observe_call_state", observe)
    return calls


async def _observed_run(journal: RecordingJournal) -> SessionResult:
    clock = FakeClock()
    return await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=FakePageSession(meet_page([IN_CALL])).open,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )


async def test_run_meeting_session_rides_out_slow_ui_observations_within_the_grace_window(
    monkeypatch: pytest.MonkeyPatch, journal: RecordingJournal
) -> None:
    _script_observations(
        monkeypatch,
        [TimeoutError(), TimeoutError(), TimeoutError()],
        then=CallObservation("ended", "the call has ended"),
    )

    result = await _observed_run(journal)

    assert result.terminal_reason == "ended_by_host"
    assert len(journal.events("watchdog_failed")) == 3
    assert journal.events("ui_observation_expired") == []


async def test_run_meeting_session_fails_once_ui_observation_stays_slow_past_the_grace_window(
    monkeypatch: pytest.MonkeyPatch, journal: RecordingJournal
) -> None:
    calls = _script_observations(monkeypatch, [], then=TimeoutError())

    result = await _observed_run(journal)

    assert result.terminal_reason == "browser_failure"
    [lost] = journal.events("ui_observation_expired")
    assert lost["failing_ms"] >= OBSERVATION_GRACE_SECONDS * 1000
    assert lost["detail"] == "TimeoutError"
    assert len(journal.events("watchdog_failed")) == calls[0]


async def test_run_meeting_session_fails_fast_when_the_page_itself_errors_twice(
    monkeypatch: pytest.MonkeyPatch, journal: RecordingJournal
) -> None:
    from playwright.async_api import Error as PlaywrightError

    _script_observations(monkeypatch, [], then=PlaywrightError("Target closed"))

    result = await _observed_run(journal)

    assert result.terminal_reason == "browser_failure"
    [lost] = journal.events("ui_observation_expired")
    assert lost["failing_ms"] < OBSERVATION_GRACE_SECONDS * 1000
    assert len(journal.events("watchdog_failed")) == 2
