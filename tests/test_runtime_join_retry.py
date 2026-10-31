import asyncio
from typing import Any, cast

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig
from fakeyou.conversation.state import LifecycleState
from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS, JoinResult
from fakeyou.orchestration.lifecycle import JoinAttempt, initial_lifecycle_state
from fakeyou.orchestration.runtime import MeetingWorker, SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import ENDED, IN_CALL, LOBBY, FakePageSession, meet_page
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import INSTANT_JOIN_RETRY, pin_join_jitter, session_config


@pytest.fixture(autouse=True)
def instant_join_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_join_jitter(monkeypatch)


class ScriptedGuestJoin:
    def __init__(self, stop: asyncio.Event, *results: JoinResult, stop_after: int = 0) -> None:
        self.stop = stop
        self.results = list(results)
        self.stop_after = stop_after
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> JoinResult:
        self.calls += 1
        if self.calls == self.stop_after:
            self.stop.set()
        return self.results.pop(0)


def _joining() -> LifecycleState:
    state = initial_lifecycle_state("owner-1", "twin-1", "meeting-1")
    return cast(LifecycleState, {**state, "phase": "joining"})


def _worker(stop: asyncio.Event, journal: RecordingJournal) -> MeetingWorker:
    clock = FakeClock()
    return MeetingWorker(
        session_config(),
        meet_page([]),
        stop,
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )


async def test_worker_retryable_attempt_defers_to_the_node_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    journal = RecordingJournal()
    guest_join = ScriptedGuestJoin(stop, JoinResult(status="timeout", detail="lobby timeout"))
    monkeypatch.setattr("fakeyou.orchestration.runtime.request_guest_join", guest_join)

    step = await _worker(stop, journal).join(_joining(), JoinAttempt(number=1, last=False))

    assert step.result.status == "timeout"
    assert step.update == {}
    assert guest_join.calls == 1
    assert journal.events("join_finished") == []
    assert [row["event"] for row in journal.events()] == ["join_requested"]


async def test_worker_stop_after_a_retryable_attempt_aborts_before_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    journal = RecordingJournal()
    guest_join = ScriptedGuestJoin(
        stop, JoinResult(status="timeout", detail="lobby timeout"), stop_after=1
    )
    monkeypatch.setattr("fakeyou.orchestration.runtime.request_guest_join", guest_join)

    step = await _worker(stop, journal).join(_joining(), JoinAttempt(number=1, last=False))

    assert step.result == JoinResult(status="aborted", detail="owner stopped before join retry")
    assert step.update["phase"] == "ended"
    assert step.update["terminal_reason"] == "stopped_by_owner"
    assert guest_join.calls == 1
    [finished] = journal.events("join_finished")
    assert finished["status"] == "aborted"
    assert finished["attempts"] == 1


async def test_worker_stop_during_backoff_aborts_the_next_attempt_without_joining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    journal = RecordingJournal()
    guest_join = ScriptedGuestJoin(stop)
    monkeypatch.setattr("fakeyou.orchestration.runtime.request_guest_join", guest_join)
    stop.set()

    step = await _worker(stop, journal).join(_joining(), JoinAttempt(number=2, last=False))

    assert step.result == JoinResult(status="aborted", detail="owner stopped before join retry")
    assert step.update["terminal_reason"] == "stopped_by_owner"
    assert step.update["external_effects"] == [{"effect": "join_requested"}]
    assert guest_join.calls == 0
    [finished] = journal.events("join_finished")
    assert finished["attempts"] == 1
    assert journal.events("join_requested") == []


async def test_worker_stop_before_the_first_attempt_never_requests_a_join(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    journal = RecordingJournal()
    guest_join = ScriptedGuestJoin(stop)
    monkeypatch.setattr("fakeyou.orchestration.runtime.request_guest_join", guest_join)
    stop.set()

    step = await _worker(stop, journal).join(_joining(), JoinAttempt(number=1, last=False))

    assert step.result == JoinResult(status="aborted", detail="owner stopped before guest join")
    assert step.update == {"phase": "ended", "terminal_reason": "stopped_by_owner"}
    assert guest_join.calls == 0
    assert journal.events() == []


async def test_worker_last_attempt_keeps_the_retryable_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    journal = RecordingJournal()
    guest_join = ScriptedGuestJoin(
        stop, JoinResult(status="timeout", detail="lobby timeout"), stop_after=1
    )
    monkeypatch.setattr("fakeyou.orchestration.runtime.request_guest_join", guest_join)

    step = await _worker(stop, journal).join(_joining(), JoinAttempt(number=3, last=True))

    assert step.result.status == "timeout"
    assert step.update["terminal_reason"] == "lobby_timeout_reached"
    [finished] = journal.events("join_finished")
    assert finished["attempts"] == 3


async def test_worker_join_retries_timeout_then_admits() -> None:
    contents = [LOBBY] * 5 + [IN_CALL, IN_CALL, ENDED]
    browser = FakePageSession(meet_page(contents))
    journal = RecordingJournal()
    clock = FakeClock()
    config = SessionConfig(
        owner_id=SessionId("owner-1"),
        twin_id=SessionId("twin-1"),
        meeting_id=SessionId("meeting-1"),
        link=LINK,
        twin_name="Twin",
        lobby_timeout_seconds=5.0,
        join_poll_interval_seconds=2.0,
        join_max_attempts=3,
        **INSTANT_JOIN_RETRY,
    )

    result = await run_meeting_session(
        config,
        SessionWiring(
            open_page=browser.open,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.terminal_reason == "ended_by_host"
    [finished] = journal.events("join_finished")
    assert finished["status"] == "admitted"
    assert finished["attempts"] == 2
    attempts = journal.events("join_attempt_finished")
    assert [(row["attempt"], row["outcome"]) for row in attempts] == [(1, "retry"), (2, "joined")]
    assert {row["source"] for row in attempts} == {"runtime"}
    assert len(journal.events("join_requested")) == 1


async def test_session_join_attempts_follow_the_configured_maximum() -> None:
    browser = FakePageSession(meet_page([LOBBY] * 15))
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(
            lobby_timeout_seconds=5.0, join_poll_interval_seconds=2.0, join_max_attempts=2
        ),
        SessionWiring(
            open_page=browser.open,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
        ),
    )

    assert result.terminal_reason == "lobby_timeout_reached"
    attempts = journal.events("join_attempt_finished")
    assert [(row["attempt"], row["outcome"]) for row in attempts] == [(1, "retry"), (2, "failed")]
    assert {row["status"] for row in attempts} == {"timeout"}
    assert all(row["detail"] for row in attempts)
    [finished] = journal.events("join_finished")
    assert finished["attempts"] == 2
