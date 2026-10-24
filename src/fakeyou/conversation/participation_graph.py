from dataclasses import dataclass
from functools import partial
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from fakeyou.conversation.participation_decision import (
    ParticipationDecision,
    to_participation_action,
)
from fakeyou.conversation.turn_events import JournalSink
from fakeyou.conversation.turn_judge import JudgeCache, JudgeWindow, TurnJudgement, WindowSource
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.observability.faults import capture_error

JudgeFailureCode = Literal["judge_timeout", "judge_schema_invalid", "judge_unavailable"]
DecisionSource = Literal["judge", "fallback", "floor"]

_DECISION_OF_ACTION: dict[str, Literal["speak", "silent", "clarify"]] = {
    "reply": "speak",
    "clarify": "clarify",
    "stay_silent": "silent",
}


@dataclass(frozen=True, slots=True)
class JudgeThresholds:
    reply: float
    clarify: float
    barge_in: float = 0.6


@dataclass(frozen=True, slots=True)
class TurnJudging:
    cache: JudgeCache
    windows: WindowSource
    thresholds: JudgeThresholds


@dataclass(frozen=True, slots=True)
class ParticipationRuntime:
    journal: JournalSink
    thresholds: JudgeThresholds
    cache: JudgeCache | None = None


class ParticipationState(TypedDict):
    window: JudgeWindow | None
    context: ParticipationContext
    transcript_id: str
    last_participation: ParticipationDecision | None
    decision_source: DecisionSource | None


ParticipationVerdict = dict[str, ParticipationDecision | DecisionSource | None]


def judge_failure_code(error: BaseException | None) -> JudgeFailureCode:
    if isinstance(error, TimeoutError):
        return "judge_timeout"
    if isinstance(error, ValidationError):
        return "judge_schema_invalid"
    return "judge_unavailable"


def decision_of(judgement: TurnJudgement, thresholds: JudgeThresholds) -> ParticipationDecision:
    decision = _DECISION_OF_ACTION[judgement.action]
    floor = {"speak": thresholds.reply, "clarify": thresholds.clarify}.get(decision)
    if floor is not None and judgement.confidence < floor:
        return ParticipationDecision(
            decision="silent",
            confidence=judgement.confidence,
            reason_code="judge_below_threshold",
            backchannel=judgement.backchannel,
        )
    return ParticipationDecision(
        decision=decision,
        confidence=judgement.confidence,
        reason_code=f"judge_{judgement.action}",
        backchannel=judgement.backchannel,
    )


def fallback_decision(context: ParticipationContext) -> ParticipationDecision:
    if context.addressed:
        return ParticipationDecision(decision="speak", confidence=1.0, reason_code="fallback_named")
    return ParticipationDecision(decision="silent", confidence=1.0, reason_code="fallback_unnamed")


def _record(
    journal: JournalSink,
    state: ParticipationState,
    verdict: ParticipationDecision,
    source: DecisionSource,
    judgement: TurnJudgement | None = None,
) -> ParticipationVerdict:
    fields: dict[str, object] = {}
    if judgement is not None:
        fields = {
            "addressee": judgement.addressee,
            "finished": judgement.finished,
            "backchannel": judgement.backchannel,
        }
    journal.record(
        "policy_decided",
        source="agent",
        transcript_id=state["transcript_id"],
        action=to_participation_action(verdict),
        reason=verdict.reason_code,
        decision_source=source,
        confidence=verdict.confidence,
        **fields,
    )
    return {"last_participation": verdict, "decision_source": source}


def build_participation_subgraph() -> CompiledStateGraph[
    ParticipationState, ParticipationRuntime, ParticipationState, ParticipationState
]:
    def hold_floor(
        state: ParticipationState, runtime: Runtime[ParticipationRuntime]
    ) -> ParticipationVerdict:
        verdict = ParticipationDecision(
            decision="silent", confidence=1.0, reason_code="floor_occupied"
        )
        return _record(runtime.context.journal, state, verdict, "floor")

    async def judge(
        state: ParticipationState, runtime: Runtime[ParticipationRuntime]
    ) -> ParticipationVerdict:
        context = runtime.context
        cache, window = context.cache, state["window"]
        if cache is None or window is None:
            return _record(context.journal, state, fallback_decision(state["context"]), "fallback")
        judgement, error = await capture_error(partial(cache.judgement, window))
        if judgement is None:
            return _fall_back(state, context, judge_failure_code(error))
        verdict = decision_of(judgement, context.thresholds)
        return _record(context.journal, state, verdict, "judge", judgement)

    builder = StateGraph(ParticipationState, context_schema=ParticipationRuntime)
    builder.add_node("hold_floor", hold_floor)
    builder.add_node("judge", judge)
    builder.add_conditional_edges(
        START,
        lambda state: "hold_floor" if state["context"].others_speaking else "judge",
        {"hold_floor": "hold_floor", "judge": "judge"},
    )
    builder.add_edge("hold_floor", END)
    builder.add_edge("judge", END)
    return builder.compile()


def _fall_back(
    state: ParticipationState, runtime: ParticipationRuntime, code: JudgeFailureCode
) -> ParticipationVerdict:
    runtime.journal.record(
        "policy_judge_failed", source="agent", transcript_id=state["transcript_id"], code=code
    )
    return _record(runtime.journal, state, fallback_decision(state["context"]), "fallback")
