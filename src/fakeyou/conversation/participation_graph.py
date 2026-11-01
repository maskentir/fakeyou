from dataclasses import dataclass
from functools import partial
from typing import Literal, TypedDict

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
    unnamed_reply: float = 0.85


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


def _silenced(judgement: TurnJudgement, reason_code: str) -> ParticipationDecision:
    return ParticipationDecision(
        decision="silent",
        confidence=judgement.confidence,
        reason_code=reason_code,
        addressee=judgement.addressee,
        backchannel=judgement.backchannel,
    )


def decision_of(
    judgement: TurnJudgement,
    thresholds: JudgeThresholds,
    *,
    named: bool = False,
    sole_human_responder: bool = False,
) -> ParticipationDecision:
    decision = _DECISION_OF_ACTION[judgement.action]
    if judgement.logistics_check and not named and not sole_human_responder:
        return _silenced(judgement, "judge_room_check")
    if decision != "silent":
        if judgement.addressee != "twin":
            return _silenced(judgement, "judge_not_addressed")
        if judgement.backchannel or not judgement.finished:
            return _silenced(judgement, "judge_not_finished")
    floor = {"speak": thresholds.reply, "clarify": thresholds.clarify}.get(decision)
    if decision == "speak" and not named:
        floor = thresholds.unnamed_reply
    if floor is not None and judgement.confidence < floor:
        return _silenced(judgement, "judge_below_threshold")
    return ParticipationDecision(
        decision=decision,
        confidence=judgement.confidence,
        reason_code=f"judge_{judgement.action}",
        addressee=judgement.addressee,
        backchannel=judgement.backchannel,
        requests_chat=judgement.requests_chat,
        meet_action=judgement.meet_action,
        evidence_needs=judgement.evidence,
        reaction_emoji=judgement.reaction_emoji,
        action_only=judgement.action_only,
        state_question=judgement.state_question,
        language=judgement.language,
    )


def fallback_decision(context: ParticipationContext) -> ParticipationDecision:
    if context.vocative or (context.vocative is None and context.addressed):
        return ParticipationDecision(decision="speak", confidence=1.0, reason_code="fallback_named")
    return ParticipationDecision(
        decision="silent", confidence=1.0, reason_code="fallback_unnamed", addressee="someone_else"
    )


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
            "heard": judgement.heard,
            "addressee": judgement.addressee,
            "finished": judgement.finished,
            "backchannel": judgement.backchannel,
            "requests_chat": judgement.requests_chat,
            "meet_action": judgement.meet_action,
            "evidence_needs": list(judgement.evidence),
            "reaction_emoji": judgement.reaction_emoji,
            "action_only": judgement.action_only,
            "logistics_check": judgement.logistics_check,
            "state_question": judgement.state_question,
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


def _hold_floor(state: ParticipationState, runtime: ParticipationRuntime) -> ParticipationVerdict:
    verdict = ParticipationDecision(
        decision="silent", confidence=1.0, reason_code="floor_occupied", addressee="everyone"
    )
    return _record(runtime.journal, state, verdict, "floor")


async def _judge(state: ParticipationState, runtime: ParticipationRuntime) -> ParticipationVerdict:
    cache, window = runtime.cache, state["window"]
    if cache is None or window is None:
        return _record(runtime.journal, state, fallback_decision(state["context"]), "fallback")
    judgement, error = await capture_error(partial(cache.judgement, window))
    if judgement is None:
        return _fall_back(state, runtime, judge_failure_code(error))
    verdict = decision_of(
        judgement,
        runtime.thresholds,
        named=state["context"].addressed,
        sole_human_responder=window.sole_human_responder,
    )
    return _record(runtime.journal, state, verdict, "judge", judgement)


async def participate(
    state: ParticipationState, runtime: ParticipationRuntime
) -> ParticipationVerdict:
    if state["context"].others_speaking:
        return _hold_floor(state, runtime)
    return await _judge(state, runtime)


def _fall_back(
    state: ParticipationState, runtime: ParticipationRuntime, code: JudgeFailureCode
) -> ParticipationVerdict:
    runtime.journal.record(
        "policy_judge_failed", source="agent", transcript_id=state["transcript_id"], code=code
    )
    return _record(runtime.journal, state, fallback_decision(state["context"]), "fallback")
