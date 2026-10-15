import asyncio
import operator
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast, get_args
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS
from fakeyou.db.checkpoint import is_checkpoint_error
from fakeyou.foundation.taxonomy import FaultKind, MeetingPhase, TerminalReason
from fakeyou.observability.faults import Fault, FaultReporter
from fakeyou.observability.journal import Journal

LifecycleAttribution = Literal["host_action", "system_fault", "owner_action", "policy"]
LifecycleEventKind = Literal[
    "infra_failed",
    "participants_left",
    "join_requested",
    "admitted",
    "denied",
    "blocked",
    "lobby_timed_out",
    "owner_stopped",
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


@dataclass(frozen=True, slots=True)
class _Transition:
    phase: MeetingPhase
    reason: TerminalReason | None = None
    effect: str | None = None
    pending_reason: TerminalReason | None = None


_TRANSITIONS: dict[tuple[MeetingPhase, str], _Transition] = {
    ("preparing", "infra_failed"): _Transition("ended", reason="infra_failure"),
    ("admitted", "participants_left"): _Transition(
        "leaving", effect="leave_requested", pending_reason="alone_limit_reached"
    ),
    ("disconnected", "participants_left"): _Transition("ended", reason="alone_limit_reached"),
    ("preparing", "join_requested"): _Transition("lobby", effect="join_requested"),
    ("preparing", "owner_stopped"): _Transition("ended", reason="stopped_by_owner"),
    ("preparing", "browser_failed"): _Transition("ended", reason="browser_failure"),
    ("preparing", "audio_failed"): _Transition("ended", reason="audio_failure"),
    ("preparing", "conversation_failed"): _Transition("ended", reason="conversation_failure"),
    ("lobby", "admitted"): _Transition("admitted"),
    ("lobby", "denied"): _Transition("ended", reason="denied_by_host"),
    ("lobby", "blocked"): _Transition("ended", reason="blocked_by_platform"),
    ("lobby", "lobby_timed_out"): _Transition("ended", reason="lobby_timeout_reached"),
    ("lobby", "owner_stopped"): _Transition("ended", reason="stopped_by_owner"),
    ("lobby", "browser_failed"): _Transition("ended", reason="browser_failure"),
    ("lobby", "audio_failed"): _Transition("ended", reason="audio_failure"),
    ("lobby", "conversation_failed"): _Transition("ended", reason="conversation_failure"),
    ("lobby", "connection_lost"): _Transition("disconnected"),
    ("admitted", "owner_stopped"): _Transition(
        "leaving", effect="leave_requested", pending_reason="stopped_by_owner"
    ),
    ("admitted", "duration_reached"): _Transition(
        "leaving", effect="leave_requested", pending_reason="duration_limit_reached"
    ),
    ("admitted", "removed"): _Transition("ended", reason="removed_by_host"),
    ("admitted", "meeting_ended"): _Transition("ended", reason="ended_by_host"),
    ("admitted", "browser_failed"): _Transition("ended", reason="browser_failure"),
    ("admitted", "audio_failed"): _Transition(
        "leaving", effect="leave_requested", pending_reason="audio_failure"
    ),
    ("admitted", "conversation_failed"): _Transition(
        "leaving", effect="leave_requested", pending_reason="conversation_failure"
    ),
    ("admitted", "connection_lost"): _Transition("disconnected"),
    ("disconnected", "owner_stopped"): _Transition("ended", reason="stopped_by_owner"),
    ("disconnected", "denied"): _Transition("ended", reason="denied_by_host"),
    ("disconnected", "lobby_timed_out"): _Transition("ended", reason="lobby_timeout_reached"),
    ("disconnected", "meeting_ended"): _Transition("ended", reason="ended_by_host"),
    ("disconnected", "removed"): _Transition("ended", reason="removed_by_host"),
    ("disconnected", "browser_failed"): _Transition("ended", reason="browser_failure"),
    ("disconnected", "audio_failed"): _Transition("ended", reason="audio_failure"),
    ("disconnected", "conversation_failed"): _Transition("ended", reason="conversation_failure"),
    ("disconnected", "duration_reached"): _Transition("ended", reason="duration_limit_reached"),
    ("leaving", "leave_sent"): _Transition("ended", reason="left_by_owner"),
    ("leaving", "owner_stopped"): _Transition("leaving"),
    ("leaving", "browser_failed"): _Transition("ended", reason="browser_failure"),
    ("leaving", "audio_failed"): _Transition("ended", reason="audio_failure"),
    ("leaving", "conversation_failed"): _Transition("ended", reason="conversation_failure"),
}


class LifecycleEvent(TypedDict):
    kind: LifecycleEventKind
    attribution: LifecycleAttribution
    detail: str
    event_id: str


class LifecycleEvidence(TypedDict):
    at: str
    from_phase: MeetingPhase
    to_phase: MeetingPhase
    event: str
    attribution: str
    detail: str
    event_id: str | None


class MeetingLifecycleState(TypedDict):
    owner_id: str
    twin_id: str
    meeting_id: str
    phase: MeetingPhase
    previous_phase: MeetingPhase | None
    pending_reason: TerminalReason | None
    terminal_reason: TerminalReason | None
    evidence: Annotated[list[LifecycleEvidence], operator.add]
    external_effects: Annotated[list[str], operator.add]
    event: NotRequired[LifecycleEvent | None]


class LifecycleUpdate(TypedDict, total=False):
    phase: MeetingPhase
    previous_phase: MeetingPhase | None
    pending_reason: TerminalReason | None
    terminal_reason: TerminalReason | None
    evidence: list[LifecycleEvidence]
    external_effects: list[str]
    event: None


def meeting_thread_id(twin_id: str, meeting_id: str) -> str:
    return f"{twin_id}:{meeting_id}"


def initial_lifecycle_state(owner_id: str, twin_id: str, meeting_id: str) -> MeetingLifecycleState:
    return {
        "owner_id": owner_id,
        "twin_id": twin_id,
        "meeting_id": meeting_id,
        "phase": "preparing",
        "previous_phase": None,
        "pending_reason": None,
        "terminal_reason": None,
        "evidence": [],
        "external_effects": [],
    }


def build_evidence(
    from_phase: MeetingPhase, to_phase: MeetingPhase, event: LifecycleEvent
) -> LifecycleEvidence:
    return {
        "at": datetime.now(UTC).isoformat(),
        "from_phase": from_phase,
        "to_phase": to_phase,
        "event": event["kind"],
        "attribution": event["attribution"],
        "detail": event["detail"],
        "event_id": event.get("event_id"),
    }


def _early_lifecycle_update(
    state: MeetingLifecycleState, phase: MeetingPhase, event: LifecycleEvent
) -> LifecycleUpdate | None:
    if phase == "ended":
        return {"event": None}
    if event["kind"] == "cancelled":
        return {
            "phase": "ended",
            "terminal_reason": "stopped_by_owner",
            "pending_reason": None,
            "previous_phase": None,
            "evidence": [build_evidence(phase, "ended", event)],
            "event": None,
        }
    if phase == "disconnected" and event["kind"] == "reconnected":
        previous = state["previous_phase"]
        if previous is None:
            raise ValueError("reconnected without a previous phase")
        return {
            "phase": previous,
            "previous_phase": None,
            "evidence": [build_evidence(phase, previous, event)],
            "event": None,
        }
    return None


def _resolve_terminal_reason(
    state: MeetingLifecycleState,
    phase: MeetingPhase,
    event: LifecycleEvent,
    transition: _Transition,
) -> TerminalReason | None:
    if phase == "leaving" and event["kind"] == "leave_sent":
        return state["pending_reason"] or "left_by_owner"
    if (
        phase == "leaving"
        and event["kind"] == "browser_failed"
        and state["pending_reason"] in ("audio_failure", "conversation_failure")
    ):
        return state["pending_reason"]
    return transition.reason


def apply_lifecycle_event(state: MeetingLifecycleState) -> LifecycleUpdate:
    event = state.get("event")
    if event is None:
        return {}
    if event["attribution"] not in _ATTRIBUTIONS:
        raise ValueError(f"unknown lifecycle attribution: {event['attribution']}")
    phase = state["phase"]
    early = _early_lifecycle_update(state, phase, event)
    if early is not None:
        return early
    transition = _TRANSITIONS.get((phase, event["kind"]))
    if transition is None:
        raise ValueError(f"invalid lifecycle event {event['kind']!r} for phase {phase!r}")
    to_phase = transition.phase
    reason = _resolve_terminal_reason(state, phase, event, transition)
    update: LifecycleUpdate = {
        "phase": to_phase,
        "evidence": [build_evidence(phase, to_phase, event)],
        "event": None,
    }
    if to_phase == "disconnected":
        update["previous_phase"] = phase
    if reason is not None:
        update["terminal_reason"] = reason
    if transition.pending_reason is not None:
        update["pending_reason"] = transition.pending_reason
    if transition.effect is not None and transition.effect not in state["external_effects"]:
        update["external_effects"] = [transition.effect]
    return update


def fold_lifecycle_events(
    state: MeetingLifecycleState, events: Sequence[LifecycleEvent]
) -> MeetingLifecycleState:
    for event in events:
        try:
            update = apply_lifecycle_event({**state, "event": event})
        except ValueError:
            continue
        merged: dict[str, Any] = dict(state)
        for key, value in update.items():
            if key in ("evidence", "external_effects") and isinstance(value, list):
                merged[key] = [*merged[key], *value]
            else:
                merged[key] = value
        state = cast(MeetingLifecycleState, merged)
    return state


def create_lifecycle_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    builder = StateGraph(MeetingLifecycleState)
    builder.add_node("apply_event", apply_lifecycle_event)
    builder.add_edge(START, "apply_event")
    builder.add_edge("apply_event", END)
    return builder.compile(checkpointer=checkpointer)


@dataclass(frozen=True, slots=True)
class LifecyclePersistence:
    send_timeout_seconds: float = DEFAULTS.database.send_timeout_seconds
    faults: FaultReporter | None = None
    journal: Journal | None = None


DEFAULT_PERSISTENCE = LifecyclePersistence()


class MeetingLifecycle:
    def __init__(
        self,
        owner_id: str,
        twin_id: str,
        meeting_id: str,
        checkpointer: BaseCheckpointSaver[Any],
        *,
        persistence: LifecyclePersistence = DEFAULT_PERSISTENCE,
    ) -> None:
        self._graph = create_lifecycle_graph(checkpointer)
        self._config: RunnableConfig = {
            "configurable": {"thread_id": meeting_thread_id(twin_id, meeting_id)}
        }
        self.state = initial_lifecycle_state(owner_id, twin_id, meeting_id)
        self._persistence = persistence
        self._pending: list[LifecycleEvent] = []

    async def initialize(self, *, resume: bool = False) -> None:
        saved = await self._graph.aget_state(self._config)
        if not saved.values:
            await self._graph.ainvoke(self.state, self._config)
            return
        for field in ("owner_id", "twin_id", "meeting_id"):
            if saved.values[field] != self.state[field]:
                raise FakeYouError(ErrorCode.MEETING_CHECKPOINT_IDENTITY_MISMATCH)
        self.state = cast(MeetingLifecycleState, saved.values)
        phase = self.state["phase"]
        if phase != "ended" and (phase != "preparing" or self.state["external_effects"]):
            if not resume:
                raise FakeYouError(
                    ErrorCode.MEETING_CANNOT_RESUME_PHASE,
                    meeting_id=self.state["meeting_id"],
                    phase=phase,
                )
            self.state = {
                **initial_lifecycle_state(
                    self.state["owner_id"], self.state["twin_id"], self.state["meeting_id"]
                ),
                "evidence": list(self.state["evidence"]),
                "external_effects": list(self.state["external_effects"]),
            }
            await self._graph.aupdate_state(
                self._config,
                {
                    "owner_id": self.state["owner_id"],
                    "twin_id": self.state["twin_id"],
                    "meeting_id": self.state["meeting_id"],
                    "phase": "preparing",
                    "previous_phase": None,
                    "pending_reason": None,
                    "terminal_reason": None,
                },
            )

    async def _reconcile_head(self, head: LifecycleEvent) -> bool:
        saved = await self._graph.aget_state(self._config)
        if not saved.values:
            return False
        applied = any(
            entry.get("event_id") == head["event_id"] for entry in saved.values["evidence"]
        )
        if not applied:
            return False
        self.state = cast(MeetingLifecycleState, saved.values)
        del self._pending[0]
        return True

    async def send(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> None:
        carried = len(self._pending)
        self._pending.append(
            {
                "kind": kind,
                "attribution": attribution,
                "detail": detail,
                "event_id": uuid4().hex,
            }
        )
        try:
            async with asyncio.timeout(self._persistence.send_timeout_seconds):
                while self._pending:
                    if carried > 0:
                        carried -= 1
                        if await self._reconcile_head(self._pending[0]):
                            continue
                    try:
                        updated = await self._graph.ainvoke(
                            {"event": self._pending[0]},
                            self._config,
                        )
                    except Exception as error:
                        if is_checkpoint_error(error):
                            raise
                        del self._pending[0]
                        raise
                    self.state = cast(MeetingLifecycleState, updated)
                    del self._pending[0]
        except Exception as error:
            if not is_checkpoint_error(error):
                raise
            if not self._pending:
                return
            if self.state["phase"] != "admitted":
                raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
            self._degrade_pending(error)

    def _degrade_pending(self, error: Exception) -> None:
        self.state = fold_lifecycle_events(self.state, self._pending)
        detail = f"lifecycle persist queued: {type(error).__name__}"
        kind: FaultKind = "timeout" if isinstance(error, TimeoutError) else "unavailable"
        if self._persistence.faults is not None:
            self._persistence.faults.record(Fault(source="runtime", kind=kind, detail=detail))
        if self._persistence.journal is not None:
            self._persistence.journal.record(
                "lifecycle_persist_queued",
                source="runtime",
                kind=self._pending[-1]["kind"],
                error_type=type(error).__name__,
                detail=detail,
            )
