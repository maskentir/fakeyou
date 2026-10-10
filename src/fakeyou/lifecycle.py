import operator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired, TypedDict, cast, get_args

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.taxonomy import TerminalReason

MeetingPhase = Literal["preparing", "lobby", "admitted", "disconnected", "leaving", "ended"]

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
    ("lobby", "admitted"): _Transition("admitted"),
    ("lobby", "denied"): _Transition("ended", reason="denied_by_host"),
    ("lobby", "blocked"): _Transition("ended", reason="blocked_by_platform"),
    ("lobby", "lobby_timed_out"): _Transition("ended", reason="lobby_timeout_reached"),
    ("lobby", "owner_stopped"): _Transition("ended", reason="stopped_by_owner"),
    ("lobby", "browser_failed"): _Transition("ended", reason="browser_failure"),
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
}


class LifecycleEvent(TypedDict):
    kind: LifecycleEventKind
    attribution: LifecycleAttribution
    detail: str


class LifecycleEvidence(TypedDict):
    at: str
    from_phase: MeetingPhase
    to_phase: MeetingPhase
    event: str
    attribution: str
    detail: str


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
    }


def apply_lifecycle_event(state: MeetingLifecycleState) -> LifecycleUpdate:
    event = state.get("event")
    if event is None:
        return {}
    if event["attribution"] not in _ATTRIBUTIONS:
        raise ValueError(f"unknown lifecycle attribution: {event['attribution']}")
    phase = state["phase"]
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
    transition = _TRANSITIONS.get((phase, event["kind"]))
    if transition is None:
        raise ValueError(f"invalid lifecycle event {event['kind']!r} for phase {phase!r}")
    to_phase, reason = transition.phase, transition.reason
    if phase == "leaving" and event["kind"] == "leave_sent":
        reason = state["pending_reason"] or "left_by_owner"
    elif (
        phase == "leaving"
        and event["kind"] == "browser_failed"
        and state["pending_reason"] in ("audio_failure", "conversation_failure")
    ):
        reason = state["pending_reason"]
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


def create_lifecycle_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    builder = StateGraph(MeetingLifecycleState)
    builder.add_node("apply_event", apply_lifecycle_event)
    builder.add_edge(START, "apply_event")
    builder.add_edge("apply_event", END)
    return builder.compile(checkpointer=checkpointer)


class MeetingLifecycle:
    def __init__(
        self, owner_id: str, twin_id: str, meeting_id: str, checkpointer: BaseCheckpointSaver[Any]
    ) -> None:
        self._graph = create_lifecycle_graph(checkpointer)
        self._config: RunnableConfig = {
            "configurable": {"thread_id": meeting_thread_id(twin_id, meeting_id)}
        }
        self.state = initial_lifecycle_state(owner_id, twin_id, meeting_id)

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

    async def send(
        self, kind: LifecycleEventKind, attribution: LifecycleAttribution, detail: str
    ) -> None:
        updated = await self._graph.ainvoke(
            {"event": {"kind": kind, "attribution": attribution, "detail": detail}},
            self._config,
        )
        self.state = cast(MeetingLifecycleState, updated)
