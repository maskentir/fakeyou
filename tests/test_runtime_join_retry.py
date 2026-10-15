from collections.abc import Callable, Coroutine, Iterable
from typing import Any

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig
from fakeyou.meet.meet import JoinResult, JoinStatus
from fakeyou.orchestration.runtime import (
    JoinRetryPolicy,
    SessionWiring,
    run_join_with_retry,
    run_meeting_session,
)
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import ENDED, IN_CALL, LOBBY, FakePageSession, meet_page
from tests._persistence_fakes import FakeSaverSession, RecordingJournal


def _scripted(
    statuses: Iterable[JoinStatus],
) -> tuple[Callable[[], Coroutine[Any, Any, JoinResult]], list[int]]:
    outcomes = [JoinResult(status=status, detail=f"{status} detail") for status in statuses]
    calls: list[int] = []

    async def attempt() -> JoinResult:
        calls.append(len(calls))
        return outcomes.pop(0)

    return attempt, calls


async def _retry(
    attempt: Callable[[], Coroutine[Any, Any, JoinResult]],
    journal: RecordingJournal,
    clock: FakeClock,
    should_stop: Callable[[], bool],
    policy: JoinRetryPolicy,
) -> tuple[JoinResult, int]:
    return await run_join_with_retry(
        attempt,
        policy=policy,
        sleep=clock.sleep,
        record=journal.record,
        should_stop=should_stop,
    )


async def test_join_retry_succeeds_after_transient_failures() -> None:
    attempt, calls = _scripted(["timeout", "failed", "admitted"])
    clock = FakeClock()
    journal = RecordingJournal()

    result, attempts = await _retry(
        attempt,
        journal,
        clock,
        lambda: False,
        JoinRetryPolicy(3, 2.0, 30.0, lambda _low, _high: 1.0),
    )

    assert result.status == "admitted"
    assert attempts == 3
    assert len(calls) == 3
    assert clock.sleeps == [1.0, 1.0]
    retried = journal.events("join_retried")
    assert [(row["attempt"], row["status"], row["delay_ms"]) for row in retried] == [
        (1, "timeout", 1000.0),
        (2, "failed", 1000.0),
    ]
    assert {row["source"] for row in retried} == {"runtime"}


@pytest.mark.parametrize("status", ["admitted", "denied", "blocked", "aborted"])
async def test_join_retry_terminal_statuses_do_not_retry(status: JoinStatus) -> None:
    attempt, calls = _scripted([status])
    clock = FakeClock()
    journal = RecordingJournal()

    result, attempts = await _retry(
        attempt,
        journal,
        clock,
        lambda: False,
        JoinRetryPolicy(3, 2.0, 30.0, lambda _low, _high: 1.0),
    )

    assert result.status == status
    assert attempts == 1
    assert len(calls) == 1
    assert clock.sleeps == []
    assert journal.events("join_retried") == []


async def test_join_retry_exhaustion_returns_last_result() -> None:
    attempt, calls = _scripted(["timeout", "failed", "timeout"])
    clock = FakeClock()
    journal = RecordingJournal()

    result, attempts = await _retry(
        attempt,
        journal,
        clock,
        lambda: False,
        JoinRetryPolicy(3, 2.0, 30.0, lambda _low, _high: 0.5),
    )

    assert result.status == "timeout"
    assert attempts == 3
    assert len(calls) == 3
    assert clock.sleeps == [0.5, 0.5]
    assert len(journal.events("join_retried")) == 2


async def test_join_retry_stop_wins_before_retry() -> None:
    attempt, calls = _scripted(["timeout", "admitted"])
    clock = FakeClock()
    journal = RecordingJournal()

    result, attempts = await _retry(
        attempt,
        journal,
        clock,
        lambda: True,
        JoinRetryPolicy(3, 2.0, 30.0, lambda _low, _high: 1.0),
    )

    assert result.status == "aborted"
    assert attempts == 1
    assert len(calls) == 1
    assert clock.sleeps == []
    assert journal.events("join_retried") == []


async def test_join_retry_stop_during_backoff_aborts() -> None:
    attempt, calls = _scripted(["timeout", "admitted"])
    clock = FakeClock()
    journal = RecordingJournal()
    stopped = False

    async def sleep_and_stop(delay: float) -> None:
        nonlocal stopped
        await clock.sleep(delay)
        stopped = True

    result, attempts = await run_join_with_retry(
        attempt,
        policy=JoinRetryPolicy(3, 2.0, 30.0, lambda _low, _high: 1.0),
        sleep=sleep_and_stop,
        record=journal.record,
        should_stop=lambda: stopped,
    )

    assert result.status == "aborted"
    assert attempts == 1
    assert len(calls) == 1
    assert len(journal.events("join_retried")) == 1


async def test_join_retry_delay_uses_capped_full_jitter() -> None:
    attempt, _calls = _scripted(["timeout", "timeout", "timeout", "admitted"])
    clock = FakeClock()
    journal = RecordingJournal()
    bounds = []
    script = [0.0, None, None]

    def scripted_rng(low: float, high: float) -> float:
        bounds.append((low, high))
        value = script.pop(0)
        return high if value is None else value

    result, attempts = await _retry(
        attempt, journal, clock, lambda: False, JoinRetryPolicy(4, 2.0, 5.0, scripted_rng)
    )

    assert result.status == "admitted"
    assert attempts == 4
    assert bounds == [(0.0, 2.0), (0.0, 4.0), (0.0, 5.0)]
    delays = [row["delay_ms"] for row in journal.events("join_retried")]
    assert delays == [0.0, 4000.0, 5000.0]
    assert len(set(delays)) > 1


async def test_join_retry_default_jitter_stays_within_bound() -> None:
    attempt, _calls = _scripted(["timeout", "admitted"])
    clock = FakeClock()
    journal = RecordingJournal()

    result, attempts = await run_join_with_retry(
        attempt,
        policy=JoinRetryPolicy(2, 2.0, 30.0),
        sleep=clock.sleep,
        record=journal.record,
        should_stop=lambda: False,
    )

    assert result.status == "admitted"
    assert attempts == 2
    [delay] = clock.sleeps
    assert 0.0 <= delay <= 2.0


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
    )

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

    assert result.terminal_reason == "ended_by_host"
    [finished] = journal.events("join_finished")
    assert finished["status"] == "admitted"
    assert finished["attempts"] == 2
    [retried] = journal.events("join_retried")
    assert retried["status"] == "timeout"
    assert retried["source"] == "runtime"
