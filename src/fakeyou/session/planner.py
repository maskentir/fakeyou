from dataclasses import dataclass, replace
from typing import Literal

from fakeyou.session.facts import StateQuestion
from fakeyou.session.intent import (
    Answer,
    ChatRequest,
    Clarify,
    Compound,
    EvidenceNeed,
    MeetAction,
    NoResponse,
)
from fakeyou.session.policy import Authorization, Channel, Refusal

Template = Literal[
    "silent",
    "speech_only",
    "evidence_then_speech",
    "action_only",
    "action_then_speech",
    "chat_only",
    "evidence_then_chat",
    "speech_then_leave",
    "chat_then_leave",
    "leave_only",
]
PlanState = Literal[
    "proposed",
    "authorized",
    "waiting",
    "running",
    "settling",
    "completed",
    "failed",
    "cancelled",
    "superseded",
]
OutputChannel = Literal["speech", "chat"]
TERMINAL_PLAN_STATES: frozenset[PlanState] = frozenset(
    {"completed", "failed", "cancelled", "superseded"}
)
_FORWARD: dict[PlanState, PlanState] = {
    "proposed": "authorized",
    "authorized": "waiting",
    "waiting": "running",
    "running": "settling",
    "settling": "completed",
}


class PlanTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TurnPlan:
    plan_id: str
    template: Template
    output: OutputChannel | None = None
    evidence: tuple[EvidenceNeed, ...] = ()
    action: MeetAction | None = None
    leave: bool = False
    clarify: bool = False
    state_question: StateQuestion | None = None
    refusals: tuple[Refusal, ...] = ()
    silence_reason: str | None = None

    @property
    def action_only(self) -> bool:
        return self.template == "action_only"


def _with_output(
    plan_id: str, output: Answer | ChatRequest | Clarify, channel: Channel
) -> TurnPlan:
    chat = isinstance(output, ChatRequest) or channel == "chat"
    evidence = () if isinstance(output, Clarify) else output.evidence
    if chat:
        template: Template = "evidence_then_chat" if evidence else "chat_only"
    else:
        template = "evidence_then_speech" if evidence else "speech_only"
    return TurnPlan(
        plan_id,
        template,
        output="chat" if chat else "speech",
        evidence=evidence,
        clarify=isinstance(output, Clarify),
        state_question=output.state if isinstance(output, Answer) else None,
    )


def _compound(plan_id: str, intent: Compound, channel: Channel) -> TurnPlan:
    chat = isinstance(intent.output, ChatRequest) or channel == "chat"
    if intent.action.kind == "leave":
        return TurnPlan(
            plan_id,
            "chat_then_leave" if chat else "speech_then_leave",
            output="chat" if chat else "speech",
            leave=True,
        )
    if chat:
        return _with_output(plan_id, Clarify(), channel)
    return TurnPlan(plan_id, "action_then_speech", output="speech", action=intent.action)


def plan_turn(authorization: Authorization, *, channel: Channel, plan_id: str) -> TurnPlan:
    intent = authorization.intent
    match intent:
        case NoResponse(reason=reason):
            plan = TurnPlan(plan_id, "silent", silence_reason=reason)
        case Answer() | ChatRequest() | Clarify():
            plan = _with_output(plan_id, intent, channel)
        case MeetAction(kind="leave"):
            plan = TurnPlan(plan_id, "leave_only", leave=True)
        case MeetAction():
            plan = TurnPlan(plan_id, "action_only", action=intent)
        case Compound():
            plan = _compound(plan_id, intent, channel)
    return replace(plan, refusals=authorization.refusals)


def advance(state: PlanState, to: PlanState) -> PlanState:
    if state in TERMINAL_PLAN_STATES:
        raise PlanTransitionError(f"plan already {state}")
    if to in TERMINAL_PLAN_STATES - {"completed"} or _FORWARD.get(state) == to:
        return to
    raise PlanTransitionError(f"plan cannot move from {state} to {to}")
