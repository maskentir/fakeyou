from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal, NamedTuple, Protocol, TypedDict, cast, get_args

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.state import ExternalEffect, LifecycleState, validate_phase_reason
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason
from fakeyou.meet.meet import JoinResult, JoinStatus

LifecycleAttribution = Literal["host_action", "system_fault", "owner_action", "policy"]
LifecycleEventKind = Literal[
    "prepared",
    "infra_failed",
    "participants_left",
    "join_requested",
    "admitted",
    "activated",
    "denied",
    "blocked",
    "lobby_timed_out",
    "owner_stopped",
    "leave_requested",
    "browser_failed",
    "audio_failed",
    "conversation_failed",
    "connection_lost",
    "reconnected",
    "duration_reached",
    "removed",
    "meeting_ended",
    "leave_sent",
    "cancelled",
]

_ATTRIBUTIONS = frozenset(get_args(LifecycleAttribution))
_LIVE: tuple[PublicPhase, ...] = ("admitted", "active")
_STARTING: tuple[PublicPhase, ...] = ("created", "joining", "lobby")


@dataclass(frozen=True, slots=True)
class _Transition:
    phase: PublicPhase
    reason: TerminalReason | None = None
    effect: str | None = None
    pending: TerminalReason | None = None


def _edges() -> dict[tuple[PublicPhase, str], _Transition]:
    edges: dict[tuple[PublicPhase, str], _Transition] = {}

    def add(phases: tuple[PublicPhase, ...], kind: str, transition: _Transition) -> None:
        for phase in phases:
            edges[(phase, kind)] = transition

    add(("created",), "prepared", _Transition("joining"))
    add(("created",), "infra_failed", _Transition("ended", reason="infra_failure"))
    add(_STARTING, "owner_stopped", _Transition("ended", reason="stopped_by_owner"))
    add(_STARTING, "browser_failed", _Transition("ended", reason="browser_failure"))
    add(_STARTING, "audio_failed", _Transition("ended", reason="audio_failure"))
    add(_STARTING, "conversation_failed", _Transition("ended", reason="conversation_failure"))
    add(("joining",), "join_requested", _Transition("lobby", effect="join_requested"))
    add(("lobby",), "admitted", _Transition("admitted"))
    add(("lobby",), "denied", _Transition("ended", reason="denied_by_host"))
    add(("lobby",), "blocked", _Transition("ended", reason="blocked_by_platform"))
    add(("lobby",), "lobby_timed_out", _Transition("ended", reason="lobby_timeout_reached"))
    add(("joining",), "denied", _Transition("ended", reason="denied_by_host"))
    add(("joining",), "lobby_timed_out", _Transition("ended", reason="lobby_timeout_reached"))
    add(("joining",), "participants_left", _Transition("ended", reason="alone_limit_reached"))
    add(("joining",), "meeting_ended", _Transition("ended", reason="ended_by_host"))
    add(("joining",), "removed", _Transition("ended", reason="removed_by_host"))
    add(("joining",), "duration_reached", _Transition("ended", reason="duration_limit_reached"))
    leaving = _Transition("leaving", effect="leave_requested")
    add(_LIVE, "participants_left", replace(leaving, pending="alone_limit_reached"))
    add(_LIVE, "owner_stopped", replace(leaving, pending="stopped_by_owner"))
    add(_LIVE, "leave_requested", replace(leaving, pending="left_by_participant"))
    add(_LIVE, "duration_reached", replace(leaving, pending="duration_limit_reached"))
    add(_LIVE, "removed", _Transition("ended", reason="removed_by_host"))
    add(_LIVE, "meeting_ended", _Transition("ended", reason="ended_by_host"))
    add(_LIVE, "browser_failed", _Transition("ended", reason="browser_failure"))
    add(_LIVE, "audio_failed", replace(leaving, pending="audio_failure"))
    add(_LIVE, "conversation_failed", replace(leaving, pending="conversation_failure"))
    add(_LIVE, "connection_lost", _Transition("joining"))
    add(("admitted",), "activated", _Transition("active"))
    add(("leaving",), "leave_sent", _Transition("ended", reason="left_by_owner"))
    add(("leaving",), "owner_stopped", _Transition("leaving"))
    add(("leaving",), "browser_failed", _Transition("ended", reason="browser_failure"))
    add(("leaving",), "audio_failed", _Transition("ended", reason="audio_failure"))
    add(("leaving",), "conversation_failed", _Transition("ended", reason="conversation_failure"))
    return edges


_TRANSITIONS: dict[tuple[PublicPhase, str], _Transition] = _edges()


class LifecycleUpdate(TypedDict, total=False):
    phase: PublicPhase
    terminal_reason: str | None
    external_effects: list[ExternalEffect]


def initial_lifecycle_state(owner_id: str, twin_id: str, meeting_id: str) -> LifecycleState:
    return {
        "owner_id": owner_id,
        "twin_id": twin_id,
        "meeting_id": meeting_id,
        "phase": "created",
        "terminal_reason": None,
        "external_effects": [],
    }


def _terminal_phase(reason: str | None) -> PublicPhase:
    return "failed" if reason == "conversation_failure" else "ended"


def _resolve_reason(
    state: LifecycleState, phase: PublicPhase, kind: str, transition: _Transition
) -> TerminalReason | None:
    if phase == "leaving" and kind == "leave_sent":
        return cast(TerminalReason | None, state["terminal_reason"]) or "left_by_owner"
    if (
        phase == "leaving"
        and kind == "browser_failed"
        and state["terminal_reason"] in ("audio_failure", "conversation_failure")
    ):
        return cast(TerminalReason, state["terminal_reason"])
    if transition.pending is not None:
        return transition.pending
    return transition.reason


def apply_transition(
    state: LifecycleState,
    kind: LifecycleEventKind,
    attribution: LifecycleAttribution,
    *,
    previous: PublicPhase | None = None,
) -> LifecycleUpdate:
    if attribution not in _ATTRIBUTIONS:
        raise ValueError(f"unknown lifecycle attribution: {attribution}")
    phase = state["phase"]
    if phase in TERMINAL_PHASES:
        return {}
    if kind == "cancelled":
        return {"phase": "ended", "terminal_reason": "stopped_by_owner"}
    if kind == "reconnected":
        if phase != "joining" or previous is None:
            raise ValueError("reconnected without a previous phase")
        return {"phase": previous}
    transition = _TRANSITIONS.get((phase, kind))
    if transition is None:
        raise ValueError(f"invalid lifecycle event {kind!r} for phase {phase!r}")
    reason = _resolve_reason(state, phase, kind, transition)
    to_phase = _terminal_phase(reason) if transition.phase == "ended" else transition.phase
    update: LifecycleUpdate = {"phase": to_phase}
    if reason is not None:
        update["terminal_reason"] = reason
    if transition.effect is not None and all(
        item["effect"] != transition.effect for item in state["external_effects"]
    ):
        update["external_effects"] = [*state["external_effects"], {"effect": transition.effect}]
    validate_phase_reason(to_phase, update.get("terminal_reason", state["terminal_reason"]))
    return update


JoinAttemptOutcome = Literal["retry", "joined", "failed", "aborted"]
JOIN_BACKOFF_FACTOR = 2.0


class JoinRetryRequested(Exception):
    def __init__(self, result: JoinResult) -> None:
        super().__init__(f"join attempt ended with {result.status}")
        self.result = result


@dataclass(frozen=True, slots=True)
class JoinAttempt:
    number: int
    last: bool

    def retries(self, result: JoinResult) -> bool:
        return not self.last and result.status == "failed" and not result.knocked


class JoinStep(NamedTuple):
    result: JoinResult
    update: LifecycleUpdate


@dataclass(frozen=True, slots=True)
class JoinRetry:
    max_attempts: int = DEFAULTS.meeting.join_max_attempts
    base_seconds: float = DEFAULTS.meeting.join_retry_base_seconds
    cap_seconds: float = DEFAULTS.meeting.join_retry_cap_seconds


def _join_outcome(status: JoinStatus, *, retrying: bool) -> JoinAttemptOutcome:
    if retrying:
        return "retry"
    if status == "admitted":
        return "joined"
    if status == "aborted":
        return "aborted"
    return "failed"


class LifecycleDriver(Protocol):
    def record(self, event: str, **fields: Any) -> None: ...

    async def prepare(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def join(self, state: LifecycleState, attempt: JoinAttempt) -> JoinStep: ...

    async def await_admission(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def activate(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def monitor(self, state: LifecycleState) -> LifecycleUpdate: ...

    async def leave(self, state: LifecycleState) -> LifecycleUpdate: ...


class SessionLifecycle:
    def __init__(self, initial: LifecycleState) -> None:
        self.state = initial

    @property
    def phase(self) -> PublicPhase:
        return self.state["phase"]

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def apply(self, update: LifecycleUpdate) -> None:
        self.state = cast(LifecycleState, {**self.state, **update})

    def send(self, kind: LifecycleEventKind, attribution: LifecycleAttribution) -> None:
        self.apply(apply_transition(self.state, kind, attribution))

    def finalize(self) -> None:
        if self.terminal:
            return
        reason = self.state["terminal_reason"] or "infra_failure"
        self.apply({"phase": _terminal_phase(reason), "terminal_reason": reason})


_SUCCESSORS: dict[str, str] = {
    "prepare": "join",
    "join": "await_admission",
    "await_admission": "activate",
    "activate": "monitor",
    "monitor": "leave",
    "leave": "finalize",
}


def _route(state: LifecycleState, step: str) -> str:
    if state["phase"] in TERMINAL_PHASES:
        return "finalize"
    if state["phase"] == "leaving":
        return "finalize" if step == "leave" else "leave"
    return _SUCCESSORS[step]


async def _join(
    driver: LifecycleDriver,
    state: LifecycleState,
    retry: JoinRetry,
    sleep: Callable[[float], Awaitable[None]],
) -> LifecycleUpdate:
    retrying = AsyncRetrying(
        stop=stop_after_attempt(retry.max_attempts),
        wait=wait_exponential_jitter(
            initial=retry.base_seconds, max=retry.cap_seconds, exp_base=JOIN_BACKOFF_FACTOR
        ),
        retry=retry_if_exception_type(JoinRetryRequested),
        sleep=sleep,
        reraise=True,
    )
    async for attempt in retrying:
        with attempt:
            number = attempt.retry_state.attempt_number
            join_attempt = JoinAttempt(number=number, last=number >= retry.max_attempts)
            step = await driver.join(state, join_attempt)
            again = join_attempt.retries(step.result)
            driver.record(
                "join_attempt_finished",
                source="runtime",
                attempt=number,
                outcome=_join_outcome(step.result.status, retrying=again),
                status=step.result.status,
                detail=step.result.detail,
            )
            if again:
                raise JoinRetryRequested(step.result)
            return step.update
    raise AssertionError("join retry ended without an outcome")


async def run_lifecycle(
    lifecycle: SessionLifecycle,
    driver: LifecycleDriver,
    *,
    retry: JoinRetry,
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    steps: dict[str, Callable[[LifecycleState], Awaitable[LifecycleUpdate]]] = {
        "prepare": driver.prepare,
        "join": lambda state: _join(driver, state, retry, sleep),
        "await_admission": driver.await_admission,
        "activate": driver.activate,
        "monitor": driver.monitor,
        "leave": driver.leave,
    }
    step = "prepare"
    while step != "finalize":
        lifecycle.apply(await steps[step](lifecycle.state))
        step = _route(lifecycle.state, step)
    lifecycle.finalize()
