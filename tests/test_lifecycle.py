from dataclasses import dataclass, field
from typing import Any, cast, get_args

import pytest

from fakeyou.conversation.state import LifecycleState, validate_phase_reason
from fakeyou.conversation.turn_graph import ConversationPhase
from fakeyou.foundation.taxonomy import MeetingPhase, PublicPhase
from fakeyou.meet.meet import JoinResult, JoinStatus
from fakeyou.orchestration.lifecycle import (
    _TRANSITIONS,
    JoinAttempt,
    JoinRetry,
    JoinStep,
    LifecycleAttribution,
    LifecycleEventKind,
    LifecycleUpdate,
    SessionLifecycle,
    apply_transition,
    initial_lifecycle_state,
    run_lifecycle,
)
from tests._persistence_fakes import RecordingJournal

PUBLIC_PHASES = frozenset(
    {"created", "joining", "lobby", "admitted", "active", "leaving", "ended", "failed"}
)
PUBLIC_TERMINAL_PHASES = frozenset({"ended", "failed"})
LEAK_MARKERS = frozenset({"ya", "sub-state"})
PHASE_MAP: tuple[tuple[str, str, str, str, str], ...] = (
    ("lobby", "lifecycle", "lobby", "—", "ya"),
    ("admitted", "lifecycle", "admitted", "—", "ya"),
    ("disconnected", "lifecycle", "joining", "—", "ya"),
    ("leaving", "lifecycle", "leaving", "—", "ya"),
    ("ended", "lifecycle", "ended", "per transisi", "ya"),
    ("listening", "conversation", "active", "—", "sub-state"),
    ("weighing", "conversation", "active", "—", "sub-state"),
    ("queued", "conversation", "active", "—", "sub-state"),
    ("interrupted", "conversation", "active", "—", "sub-state"),
    ("waiting", "conversation", "active", "—", "sub-state"),
    ("acting", "conversation", "active", "—", "sub-state"),
    ("failed", "conversation", "failed", "conversation_failure", "ya"),
    ("closed", "conversation", "leaving", "—", "sub-state"),
)
JOIN_MAX_ATTEMPTS = 3
PINNED_JITTER = 0.25
FAST_JOIN_RETRY = JoinRetry(max_attempts=JOIN_MAX_ATTEMPTS, base_seconds=0.001, cap_seconds=0.001)
_SCRIPTED_JOIN_EVENTS: dict[JoinStatus, tuple[LifecycleEventKind, LifecycleAttribution]] = {
    "denied": ("denied", "host_action"),
    "blocked": ("blocked", "system_fault"),
    "timeout": ("lobby_timed_out", "system_fault"),
    "aborted": ("owner_stopped", "owner_action"),
    "failed": ("browser_failed", "system_fault"),
}


def scripted_join_step(state: LifecycleState, result: JoinResult) -> JoinStep:
    requested = apply_transition(state, "join_requested", "owner_action")
    if result.status == "admitted":
        return JoinStep(result, requested)
    kind, attribution = _SCRIPTED_JOIN_EVENTS[result.status]
    settled = apply_transition(cast(LifecycleState, {**state, **requested}), kind, attribution)
    return JoinStep(result, {**requested, **settled})


class ScriptedDriver:
    def __init__(self, failures_before_success: int = 0, *, failure: JoinStatus = "failed") -> None:
        self.failures_before_success = failures_before_success
        self.failure = failure
        self.knocked = False
        self.seen: list[JoinAttempt] = []
        self.phases: list[PublicPhase] = []
        self.journal = RecordingJournal()

    def record(self, event: str, **fields: Any) -> None:
        self.journal.record(event, **fields)

    def outcomes(self) -> list[tuple[int, str]]:
        return [
            (row["attempt"], row["outcome"]) for row in self.journal.events("join_attempt_finished")
        ]

    async def prepare(self, state: LifecycleState) -> LifecycleUpdate:
        self.phases.append(state["phase"])
        return apply_transition(state, "prepared", "owner_action")

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep:
        self.phases.append(state["phase"])
        self.seen.append(attempt)
        status = "admitted" if len(self.seen) > self.failures_before_success else self.failure
        result = JoinResult(status=status, detail=f"{status} detail", knocked=self.knocked)
        return scripted_join_step(state, result)

    async def await_admission(self, state: LifecycleState) -> LifecycleUpdate:
        self.phases.append(state["phase"])
        return apply_transition(state, "admitted", "host_action")

    async def activate(self, state: LifecycleState) -> LifecycleUpdate:
        self.phases.append(state["phase"])
        return apply_transition(state, "activated", "policy")

    async def monitor(self, state: LifecycleState) -> LifecycleUpdate:
        self.phases.append(state["phase"])
        return apply_transition(state, "owner_stopped", "owner_action")

    async def leave(self, state: LifecycleState) -> LifecycleUpdate:
        self.phases.append(state["phase"])
        return {}


@dataclass
class JitterPin:
    value: float = 0.0
    draws: list[tuple[float, float]] = field(default_factory=list)


@pytest.fixture
def pinned_join_jitter(monkeypatch: pytest.MonkeyPatch) -> JitterPin:
    pin = JitterPin()

    def uniform(low: float, high: float) -> float:
        pin.draws.append((low, high))
        return pin.value

    monkeypatch.setattr("tenacity.wait.random.uniform", uniform)
    return pin


def _lifecycle(phase: PublicPhase = "created", **fields: Any) -> SessionLifecycle:
    return SessionLifecycle(
        cast(
            LifecycleState,
            {**initial_lifecycle_state("owner-1", "twin-1", "meeting-1"), "phase": phase, **fields},
        )
    )


def _in_phase(phase: PublicPhase, **fields: Any) -> LifecycleState:
    return _lifecycle(phase, **fields).state


async def _run(
    driver: ScriptedDriver, retry: JoinRetry = FAST_JOIN_RETRY
) -> tuple[SessionLifecycle, list[float]]:
    lifecycle = _lifecycle()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    await run_lifecycle(lifecycle, driver, retry=retry, sleep=sleep)
    return lifecycle, slept


async def test_the_lifecycle_walks_the_public_phases_in_order() -> None:
    driver = ScriptedDriver()

    lifecycle, _slept = await _run(driver)

    assert driver.phases == ["created", "joining", "lobby", "admitted", "active", "leaving"]
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "stopped_by_owner")
    assert lifecycle.state["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


async def test_a_terminal_step_skips_straight_to_the_end() -> None:
    driver = ScriptedDriver(failures_before_success=99, failure="denied")

    lifecycle, _slept = await _run(driver)

    assert driver.phases == ["created", "joining"]
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "denied_by_host")


def test_finalize_ends_a_session_that_stopped_without_a_terminal_phase() -> None:
    lifecycle = _lifecycle("leaving", terminal_reason="audio_failure")

    lifecycle.finalize()

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "audio_failure")
    empty = _lifecycle("active")
    empty.finalize()
    assert (empty.phase, empty.state["terminal_reason"]) == ("ended", "infra_failure")


def test_lifecycle_join_and_leave_happy_path() -> None:
    lifecycle = _lifecycle("joining")
    phases: list[PublicPhase] = []
    for kind, attribution in (
        ("join_requested", "owner_action"),
        ("admitted", "host_action"),
        ("activated", "policy"),
        ("owner_stopped", "owner_action"),
        ("leave_sent", "owner_action"),
    ):
        lifecycle.send(cast(LifecycleEventKind, kind), cast(LifecycleAttribution, attribution))
        phases.append(lifecycle.phase)

    assert phases == ["lobby", "admitted", "active", "leaving", "ended"]

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "stopped_by_owner")
    assert lifecycle.state["external_effects"] == [
        {"effect": "join_requested"},
        {"effect": "leave_requested"},
    ]


@pytest.mark.parametrize(
    ("kind", "attribution", "reason"),
    [
        ("denied", "host_action", "denied_by_host"),
        ("blocked", "system_fault", "blocked_by_platform"),
        ("lobby_timed_out", "system_fault", "lobby_timeout_reached"),
    ],
)
def test_lobby_outcomes_end_the_session(
    kind: LifecycleEventKind, attribution: LifecycleAttribution, reason: str
) -> None:
    lifecycle = _lifecycle("lobby")

    lifecycle.send(kind, attribution)

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", reason)


def test_lifecycle_removed_by_host() -> None:
    lifecycle = _lifecycle("admitted")

    lifecycle.send("removed", "host_action")

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "removed_by_host")


def test_lifecycle_reconnect_returns_to_previous_phase() -> None:
    lifecycle = _lifecycle("admitted")
    lifecycle.send("connection_lost", "system_fault")
    assert lifecycle.phase == "joining"

    restored = apply_transition(lifecycle.state, "reconnected", "system_fault", previous="admitted")

    assert restored == {"phase": "admitted"}


def test_lifecycle_duration_limit_leaves_with_reason() -> None:
    lifecycle = _lifecycle("admitted")

    lifecycle.send("duration_reached", "policy")
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == (
        "leaving",
        "duration_limit_reached",
    )
    lifecycle.send("leave_sent", "policy")

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == (
        "ended",
        "duration_limit_reached",
    )


def test_lifecycle_duration_limit_ends_disconnected_session() -> None:
    lifecycle = _lifecycle("admitted")
    lifecycle.send("connection_lost", "system_fault")

    lifecycle.send("duration_reached", "policy")

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == (
        "ended",
        "duration_limit_reached",
    )


def test_lifecycle_ended_ignores_further_events() -> None:
    lifecycle = _lifecycle("created")
    lifecycle.send("prepared", "owner_action")
    lifecycle.send("owner_stopped", "owner_action")
    before = lifecycle.state

    lifecycle.send("admitted", "host_action")

    assert lifecycle.state == before
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "stopped_by_owner")


def test_a_cancelled_session_ends_as_stopped_by_the_owner() -> None:
    lifecycle = _lifecycle("active")

    lifecycle.send("cancelled", "owner_action")

    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "stopped_by_owner")


def test_lifecycle_rejects_invalid_event_for_phase() -> None:
    with pytest.raises(ValueError, match="invalid lifecycle event"):
        _lifecycle("created").send("admitted", "host_action")


@pytest.mark.parametrize(
    ("kind", "reason"),
    [("audio_failed", "audio_failure"), ("conversation_failed", "conversation_failure")],
)
@pytest.mark.parametrize(
    "drive",
    [
        [],
        ["prepared", "join_requested"],
        ["prepared", "join_requested", ("admitted", "host_action"), "owner_stopped"],
    ],
    ids=["preparing", "lobby", "leaving"],
)
def test_startup_and_teardown_failures_end_the_session(
    kind: str, reason: str, drive: list[str | tuple[str, str]]
) -> None:
    lifecycle = _lifecycle("created")
    for step in drive:
        if isinstance(step, tuple):
            lifecycle.send(cast(LifecycleEventKind, step[0]), cast(LifecycleAttribution, step[1]))
        else:
            lifecycle.send(cast(LifecycleEventKind, step), "owner_action")

    lifecycle.send(cast(LifecycleEventKind, kind), "system_fault")

    expected_phase = "failed" if reason == "conversation_failure" else "ended"
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == (expected_phase, reason)


def test_lifecycle_rejects_unknown_attribution() -> None:
    with pytest.raises(ValueError, match="unknown lifecycle attribution"):
        apply_transition(_in_phase("created"), "prepared", cast(LifecycleAttribution, "mystery"))


def _phase_map_rows() -> list[list[str]]:
    return [list(row) for row in PHASE_MAP]


def test_phase_map_covers_every_phase_once() -> None:
    mapped = [row[0] for row in _phase_map_rows()]
    expected = set(get_args(ConversationPhase)) | set(get_args(MeetingPhase))

    assert {phase for phase, _event in _TRANSITIONS} <= PUBLIC_PHASES
    assert set(mapped) == expected
    assert len(mapped) == len(set(mapped))


def test_phase_map_uses_only_frozen_public_phases() -> None:
    for _old, _source, public, reason, leak in _phase_map_rows():
        assert public in PUBLIC_PHASES
        assert leak in LEAK_MARKERS
        if public in PUBLIC_TERMINAL_PHASES:
            assert reason != "—"
        else:
            assert reason == "—"


def test_phase_map_sources_match_the_enums() -> None:
    sources = {old: source for old, source, *_ in _phase_map_rows()}

    assert {sources[phase] for phase in get_args(ConversationPhase)} == {"conversation"}
    assert {sources[phase] for phase in get_args(MeetingPhase)} == {"lifecycle"}


def test_terminal_phase_requires_reason() -> None:
    with pytest.raises(ValueError, match="terminal_reason"):
        validate_phase_reason("failed", None)
    validate_phase_reason("failed", "join_failed")
    validate_phase_reason("active", None)


def test_join_retry_defaults_freeze_the_retired_loop_numbers() -> None:
    assert JoinRetry() == JoinRetry(max_attempts=3, base_seconds=2.0, cap_seconds=30.0)


async def test_join_retries_transient_errors_then_succeeds(
    pinned_join_jitter: JitterPin,
) -> None:
    driver = ScriptedDriver(failures_before_success=2)

    lifecycle, _slept = await _run(driver)

    assert lifecycle.phase == "ended"
    assert [(attempt.number, attempt.last) for attempt in driver.seen] == [
        (1, False),
        (2, False),
        (3, True),
    ]
    assert driver.outcomes() == [(1, "retry"), (2, "retry"), (3, "joined")]
    assert {row["source"] for row in driver.journal.events("join_attempt_finished")} == {"runtime"}
    assert pinned_join_jitter.draws == [(0, 1), (0, 1)]


async def test_join_retry_exhaustion_fails_with_reason(pinned_join_jitter: JitterPin) -> None:
    driver = ScriptedDriver(failures_before_success=99, failure="failed")

    lifecycle, _slept = await _run(driver)

    assert len(driver.seen) == JOIN_MAX_ATTEMPTS
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "browser_failure")
    assert driver.outcomes() == [(1, "retry"), (2, "retry"), (3, "failed")]
    assert len(pinned_join_jitter.draws) == JOIN_MAX_ATTEMPTS - 1


async def test_owner_abort_is_not_retried(pinned_join_jitter: JitterPin) -> None:
    driver = ScriptedDriver(failures_before_success=99, failure="aborted")

    lifecycle, _slept = await _run(driver)

    assert len(driver.seen) == 1
    assert (lifecycle.phase, lifecycle.state["terminal_reason"]) == ("ended", "stopped_by_owner")
    assert driver.outcomes() == [(1, "aborted")]
    assert pinned_join_jitter.draws == []


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("denied", "denied_by_host"),
        ("blocked", "blocked_by_platform"),
        ("timeout", "lobby_timeout_reached"),
    ],
)
async def test_domain_join_outcomes_end_on_the_first_attempt(
    pinned_join_jitter: JitterPin, failure: JoinStatus, reason: str
) -> None:
    driver = ScriptedDriver(failures_before_success=99, failure=failure)

    lifecycle, _slept = await _run(driver)

    assert len(driver.seen) == 1
    assert lifecycle.state["terminal_reason"] == reason
    assert driver.outcomes() == [(1, "failed")]
    assert pinned_join_jitter.draws == []


async def test_join_backoff_never_sleeps_past_the_cap(pinned_join_jitter: JitterPin) -> None:
    pinned_join_jitter.value = PINNED_JITTER
    driver = ScriptedDriver(failures_before_success=3, failure="failed")

    lifecycle, slept = await _run(
        driver, JoinRetry(max_attempts=4, base_seconds=2.0, cap_seconds=5.0)
    )

    assert lifecycle.phase == "ended"
    assert driver.outcomes() == [(1, "retry"), (2, "retry"), (3, "retry"), (4, "joined")]
    assert slept == [2.0 + PINNED_JITTER, 4.0 + PINNED_JITTER, 5.0]


async def test_a_failure_after_the_knock_is_not_retried(pinned_join_jitter: JitterPin) -> None:
    driver = ScriptedDriver(failures_before_success=99, failure="failed")
    driver.knocked = True

    lifecycle, _slept = await _run(driver)

    assert len(driver.seen) == 1
    assert lifecycle.state["terminal_reason"] == "browser_failure"
    assert driver.outcomes() == [(1, "failed")]
    assert pinned_join_jitter.draws == []
