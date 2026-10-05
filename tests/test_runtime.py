import asyncio
import json
import time
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.lifecycle import MeetingLifecycle, create_lifecycle_graph, initial_lifecycle_state
from fakeyou.meet import DEFAULT_MEET_SIGNALS
from fakeyou.participant_observer import OBSERVE_JS
from fakeyou.runtime import _MeetingWorker, run_meeting_session
from fakeyou.session import SessionConfig
from tests.fakes import (
    ASK_SELECTOR,
    DENIED,
    ENDED,
    EXPLICIT_DENIED,
    IN_CALL,
    LEAVE_SELECTOR,
    LINK,
    LOBBY,
    NAME_SELECTOR,
    NO_CAPTCHA,
    REMOVED,
    FakeAudio,
    FakeClock,
    FakePageSession,
    FakeSaverSession,
    RecordingJournal,
    meet_page,
)


class _BlockingTerminalSaver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.terminal_started = asyncio.Event()
        self.release_terminal = asyncio.Event()
        self.terminal_persisted = asyncio.Event()
        self.terminal_cancelled = asyncio.Event()
        self._blocked = False

    def factory(self):
        saver = self

        @asynccontextmanager
        async def _session():
            yield saver

        return _session()

    async def aput(self, config, checkpoint, metadata, new_versions):
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


def _config(**overrides):
    values = {
        "owner_id": "owner-1",
        "twin_id": "twin-1",
        "meeting_id": "meeting-1",
        "link": LINK,
        "twin_name": "Twin",
    }
    values.update(overrides)
    return SessionConfig(**values)


async def _run(config, browser, saver, clock, stop=None):
    return await run_meeting_session(
        config,
        stop=stop,
        open_page=browser.open,
        saver_factory=saver.factory,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


async def test_run_meeting_session_aborts_join_when_stop_requested() -> None:
    browser = FakePageSession(meet_page([]))
    saver = FakeSaverSession()
    stop = asyncio.Event()
    stop.set()

    result = await _run(_config(headless=True), browser, saver, FakeClock(), stop)

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

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock(), stop)

    assert result.phase == "ended"
    assert result.terminal_reason == "stopped_by_owner"


async def test_run_meeting_session_enforces_duration_limit() -> None:
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED]))

    result = await _run(
        _config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
        browser,
        FakeSaverSession(),
        FakeClock(),
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "duration_limit_reached"


async def test_run_meeting_session_logs_slow_monitor_poll(capsys) -> None:
    clock = FakeClock()

    def advance_on_monitor_poll(reads):
        if reads == 2:
            clock.now += 2.5

    page = meet_page([IN_CALL, IN_CALL, ENDED], on_content=advance_on_monitor_poll)
    browser = FakePageSession(page)

    result = await _run(_config(), browser, FakeSaverSession(), clock)

    assert result.terminal_reason == "ended_by_host"
    polls = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if json.loads(line).get("event") == "monitor_polled"
    ]
    assert len(polls) == 1
    assert polls[0]["meeting_id"] == "meeting-1"
    assert polls[0]["duration_ms"] == 2500


async def test_run_meeting_session_skips_monitor_poll_log_when_fast(capsys) -> None:
    browser = FakePageSession(meet_page([IN_CALL, IN_CALL, ENDED]))

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock())

    assert result.terminal_reason == "ended_by_host"
    assert "monitor_polled" not in capsys.readouterr().out


async def test_run_meeting_session_recovers_from_transient_lobby() -> None:
    browser = FakePageSession(meet_page([IN_CALL, LOBBY, IN_CALL, REMOVED]))

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "removed_by_host"


async def test_run_meeting_session_enforces_duration_limit_while_disconnected() -> None:
    page = meet_page([IN_CALL, LOBBY, LOBBY, LOBBY])
    browser = FakePageSession(page)

    result = await _run(
        _config(session_duration_seconds=5.0, monitor_interval_seconds=2.0),
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

    result = await _run(
        _config(leave_timeout_seconds=3.0), browser, FakeSaverSession(), FakeClock(), stop
    )

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_maps_observation_crash_to_browser_failure() -> None:
    browser = FakePageSession(meet_page([IN_CALL, RuntimeError("boom")]))

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "browser_failure"


async def test_run_meeting_session_tolerates_single_observation_blip() -> None:
    browser = FakePageSession(meet_page([IN_CALL, RuntimeError("boom"), IN_CALL, ENDED]))

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock())

    assert result.phase == "ended"
    assert result.terminal_reason == "ended_by_host"


async def test_run_meeting_session_returns_ended_result_without_reopening_browser() -> None:
    saver = FakeSaverSession()
    first = FakePageSession(meet_page([DENIED], evaluates=[NO_CAPTCHA]))

    result = await _run(_config(), first, saver, FakeClock())

    assert result.terminal_reason == "blocked_by_platform"
    second = FakePageSession(meet_page([LOBBY]))
    again = await _run(_config(), second, saver, FakeClock())

    assert again.phase == "ended"
    assert again.terminal_reason == "blocked_by_platform"
    assert second.opened == 0


async def test_run_meeting_session_rejects_resume_of_interrupted_session() -> None:
    saver = FakeSaverSession()
    graph = create_lifecycle_graph(checkpointer=saver.saver)
    graph.invoke(
        {
            **initial_lifecycle_state("owner-1", "twin-1", "meeting-1"),
            "event": {"kind": "join_requested", "attribution": "owner_action", "detail": "t"},
        },
        {"configurable": {"thread_id": "twin-1:meeting-1"}},
    )
    browser = FakePageSession(meet_page([LOBBY]))

    with pytest.raises(ValueError, match="cannot resume"):
        await _run(_config(), browser, saver, FakeClock())

    assert browser.opened == 0


async def test_browser_startup_failure_is_persisted_as_terminal() -> None:
    saver = FakeSaverSession()

    @asynccontextmanager
    async def fail_open(**kwargs):
        raise RuntimeError("browser launch failed")
        yield

    result = await run_meeting_session(_config(), open_page=fail_open, saver_factory=saver.factory)

    assert result.terminal_reason == "browser_failure"
    state = await create_lifecycle_graph(saver.saver).aget_state(
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
    async def open_then_stop(**kwargs):
        async with browser.open(**kwargs) as opened:
            stop.set()
            yield opened

    result = await run_meeting_session(
        _config(), stop=stop, open_page=open_then_stop, saver_factory=saver.factory
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

    result = await _run(_config(), browser, FakeSaverSession(), FakeClock(), stop)

    assert result.terminal_reason == "browser_failure"
    assert browser.closed == 1


async def test_session_rejects_checkpoint_owned_by_another_owner() -> None:
    saver = FakeSaverSession()
    first = FakePageSession(meet_page([DENIED], evaluates=[NO_CAPTCHA]))
    await _run(_config(), first, saver, FakeClock())
    other = FakePageSession(meet_page())

    with pytest.raises(ValueError, match="identity"):
        await _run(_config(owner_id="other-owner"), other, saver, FakeClock())

    assert other.opened == 0


async def test_stop_cancels_pending_navigation_and_closes_browser() -> None:
    stop = asyncio.Event()
    navigation_started = asyncio.Event()
    navigation_cancelled = asyncio.Event()
    page = meet_page()

    async def slow_navigation(*args, **kwargs):
        navigation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            navigation_cancelled.set()

    page.goto = slow_navigation
    browser = FakePageSession(page)
    async with asyncio.TaskGroup() as tasks:
        running = tasks.create_task(_run(_config(), browser, FakeSaverSession(), FakeClock(), stop))
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
            _config(monitor_interval_seconds=0.01),
            open_page=browser.open,
            saver_factory=saver.factory,
            sleep=paced_sleep,
            monotonic=time.monotonic,
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

    state = await create_lifecycle_graph(saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert state.values["phase"] == "ended"
    assert state.values["terminal_reason"] == "stopped_by_owner"
    assert browser.closed == 1


async def test_cancellation_during_leave_preserves_cancellation_and_terminal_checkpoint() -> None:
    stop = asyncio.Event()
    leaving = asyncio.Event()
    page = meet_page()
    saver = FakeSaverSession()

    async def content():
        if not stop.is_set():
            stop.set()
            return IN_CALL
        leaving.set()
        await asyncio.Event().wait()

    page.content = content
    browser = FakePageSession(page)
    running = asyncio.create_task(_run(_config(), browser, saver, FakeClock(), stop))
    try:
        await asyncio.wait_for(leaving.wait(), timeout=1)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(running, timeout=1)
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    saved = await create_lifecycle_graph(saver.saver).aget_state(
        {"configurable": {"thread_id": "twin-1:meeting-1"}}
    )
    assert saved.values["phase"] == "ended"
    assert saved.values["terminal_reason"] == "stopped_by_owner"
    assert saved.values["evidence"][-1]["event"] == "cancelled"
    assert page.clicked.count(LEAVE_SELECTOR) == 1
    assert browser.closed == 1


@pytest.mark.parametrize("field", ["owner_id", "twin_id", "meeting_id"])
@pytest.mark.parametrize("value", ["", "../another-profile", "contains:separator"])
def test_session_rejects_invalid_identity(field, value) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


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
def test_session_rejects_invalid_timing(field, value) -> None:
    with pytest.raises(ValueError):
        _config(**{field: value})


async def test_monitor_poll_should_persist_through_the_supplied_journal(tmp_path):
    from fakeyou.journal import Journal
    from fakeyou.report import read_journal

    clock = FakeClock()

    def advance_on_monitor_poll(reads):
        if reads == 2:
            clock.now += 2.5

    browser = FakePageSession(
        meet_page([IN_CALL, IN_CALL, ENDED], on_content=advance_on_monitor_poll)
    )
    saver = FakeSaverSession()
    path = tmp_path / "events.jsonl"
    journal = Journal("journal-meeting", path=path, stdout=False)
    await run_meeting_session(
        _config(),
        open_page=browser.open,
        saver_factory=saver.factory,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        journal=journal,
    )
    journal.close()

    polls = [row for row in read_journal(path) if row["event"] == "monitor_polled"]
    assert len(polls) == 1
    assert polls[0]["meeting_id"] == "journal-meeting"
    assert polls[0]["duration_ms"] == 2500
    assert polls[0]["at"]


@pytest.mark.parametrize(
    "contents,evaluates,counts,overrides,expected,detail",
    [
        ([EXPLICIT_DENIED], [], {}, {}, "denied_by_host", ""),
        ([DENIED], [NO_CAPTCHA], {}, {}, "blocked_by_platform", ""),
        (
            [LOBBY] * 4,
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
    contents, evaluates, counts, overrides, expected, detail
):
    browser = FakePageSession(meet_page(contents, evaluates=evaluates, counts=counts))
    result = await _run(_config(**overrides), browser, FakeSaverSession(), FakeClock())
    assert result.phase == "ended"
    assert result.terminal_reason == expected
    assert detail in result.detail
    assert browser.closed == 1


def _mining_payload():
    return {
        "tiles": [{"key": "a", "label": "Budi", "self": False, "speaking": True}],
        "people": [],
        "selfSeen": False,
    }


async def test_run_meeting_session_collects_participant_evidence(tmp_path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL, IN_CALL, ENDED],
        evaluates=[_mining_payload() for _ in range(20)],
    )
    browser = FakePageSession(page)
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        _config(participant_policy_enabled=True, participant_evidence_path=evidence),
        open_page=browser.open,
        saver_factory=FakeSaverSession().factory,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        journal=journal,
    )

    assert result.phase == "ended"
    assert OBSERVE_JS in page.evaluate_calls
    lines = evidence.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["method"] == "dom_mining"
    assert record["epoch"] == "meeting-1:e1"
    assert record["tiles"][0]["key"] == "a"
    started = journal.events("participant_observer_started")
    assert len(started) == 1
    assert "Budi" not in json.dumps(started)
    calls_after_finish = len(page.evaluate_calls)
    await asyncio.sleep(0.6)
    assert len(page.evaluate_calls) == calls_after_finish


async def test_run_meeting_session_skips_observer_when_flag_off(tmp_path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    page = meet_page([IN_CALL, IN_CALL, ENDED])
    browser = FakePageSession(page)

    result = await _run(
        _config(participant_evidence_path=evidence), browser, FakeSaverSession(), FakeClock()
    )

    assert result.phase == "ended"
    assert OBSERVE_JS not in page.evaluate_calls
    assert not evidence.exists()


async def test_owner_stop_produces_no_observer_or_audio_followups(tmp_path) -> None:
    evidence = tmp_path / "meeting-1" / "participants.jsonl"
    stop = asyncio.Event()
    clock = FakeClock()
    page = meet_page([], evaluates=[_mining_payload() for _ in range(10)])
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    worker = _MeetingWorker(
        _config(participant_policy_enabled=True, participant_evidence_path=evidence),
        lifecycle,
        page,
        stop,
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    worker.audio = FakeAudio([])

    await worker.start_participant_observer()
    assert worker.observer is not None
    await asyncio.sleep(0.6)
    assert evidence.exists()
    assert len(evidence.read_text(encoding="utf-8").splitlines()) >= 2

    stop.set()
    detail = await worker.monitor(clock.monotonic())

    assert detail == "owner requested stop"
    assert worker.observer is None
    assert worker.audio.events == ["audio_stopped"]
    lines_before = evidence.read_text(encoding="utf-8").splitlines()
    polls_before = len(page.evaluate_calls)
    await asyncio.sleep(0.6)
    assert evidence.read_text(encoding="utf-8").splitlines() == lines_before
    assert len(page.evaluate_calls) == polls_before
    assert worker.audio.events == ["audio_stopped"]
