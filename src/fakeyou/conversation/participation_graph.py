from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from pydantic import ValidationError

from fakeyou.conversation.participation_decider import (
    DeciderTurn,
    ParticipationDecision,
    to_participation_action,
)
from fakeyou.conversation.turn_events import JournalSink
from fakeyou.foundation.participation import (
    ParticipationContext,
    RuleDecision,
    choose_participation,
)

DeciderFailureCode = Literal[
    "decider_no_credentials",
    "decider_timeout",
    "decider_schema_invalid",
    "decider_unavailable",
]
JevKey = tuple[str | bool, ...]
DecisionSource = Literal["rule", "jev", "rules"]


class DeciderFailure(Exception):
    def __init__(self, code: DeciderFailureCode, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class JevReader(Protocol):
    @property
    def speak_threshold(self) -> float: ...

    @property
    def clarify_threshold(self) -> float: ...

    def decision(self, key: JevKey) -> ParticipationDecision | None: ...

    def failure(self, key: JevKey) -> DeciderFailure | None: ...


@dataclass(frozen=True, slots=True)
class ParticipationRuntime:
    journal: JournalSink
    jev: JevReader | None = None


class ParticipationState(TypedDict):
    turns: list[DeciderTurn]
    context: ParticipationContext
    rule_decision: ParticipationDecision | None
    model_decision: ParticipationDecision | None
    last_participation: ParticipationDecision | None
    transcript_id: str
    jev_key: JevKey
    decision_source: DecisionSource | None


_JEV_REASON: dict[str, str] = {
    "speak": "jev_speak",
    "silent": "jev_silent",
    "clarify": "jev_clarify",
}


ParticipationVerdict = dict[str, ParticipationDecision | DecisionSource | None]


def decider_window[T](turns: Sequence[T], window_turns: int) -> tuple[T, ...]:
    if window_turns < 1:
        raise ValueError("window_turns must be >= 1")
    return tuple(turns[-window_turns:])


def classify_decider_failure(error: Exception) -> DeciderFailure:
    if isinstance(error, DeciderFailure):
        return error
    if isinstance(error, TimeoutError):
        code: DeciderFailureCode = "decider_timeout"
    elif isinstance(error, ValidationError):
        code = "decider_schema_invalid"
    else:
        code = "decider_unavailable"
    return DeciderFailure(code, detail=type(error).__name__)


_RULE_TO_DECISION: dict[str, Literal["speak", "silent", "clarify"]] = {
    "reply": "speak",
    "silent": "silent",
    "clarify": "clarify",
}


def _rule_as_decision(rule: RuleDecision) -> ParticipationDecision:
    return ParticipationDecision(
        decision=_RULE_TO_DECISION[rule.action],
        confidence=1.0,
        reason_code=rule.reason,
    )


def _rule_fallback(rule: ParticipationDecision) -> ParticipationDecision:
    return rule.model_copy(update={"reason_code": f"rules_fallback:{rule.reason_code}"})


def _consults_model(state: ParticipationState) -> bool:
    rule = state["rule_decision"]
    context = state["context"]
    if rule is None or rule.reason_code != "not_addressed" or rule.decision != "silent":
        return False
    return context.turn_mine or context.turn_cue != "none"


def _record_policy(
    journal: JournalSink,
    state: ParticipationState,
    verdict: ParticipationDecision,
    *,
    reason: str,
    decision_source: DecisionSource,
) -> None:
    journal.record(
        "policy_decided",
        source="agent",
        transcript_id=state["transcript_id"],
        action=to_participation_action(verdict),
        reason=reason,
        decision_source=decision_source,
        confidence=verdict.confidence,
        turn_cue=state["context"].turn_cue,
    )


def _fallback_verdict(
    state: ParticipationState,
    journal: JournalSink,
    failure: DeciderFailure,
) -> ParticipationVerdict:
    rule = state["rule_decision"]
    if rule is None:
        raise RuntimeError("the cache verdict runs only after the rules node")
    journal.record(
        "policy_decider_failed", source="agent", code=failure.code, decision_source="rules"
    )
    fallback = _rule_fallback(rule)
    _record_policy(journal, state, fallback, reason=fallback.reason_code, decision_source="rules")
    return {"last_participation": fallback, "decision_source": "rules"}


def _verdict_from_cache(
    state: ParticipationState, runtime: Runtime[ParticipationRuntime]
) -> ParticipationVerdict:
    journal = runtime.context.journal
    reader = runtime.context.jev
    cached = None if reader is None else reader.decision(state["jev_key"])
    if reader is None or cached is None:
        failure = (None if reader is None else reader.failure(state["jev_key"])) or DeciderFailure(
            "decider_timeout", "no decision ready for this window"
        )
        return _fallback_verdict(state, journal, failure)
    threshold = reader.speak_threshold if cached.decision == "speak" else reader.clarify_threshold
    if cached.decision != "silent" and cached.confidence < threshold:
        silenced = ParticipationDecision(
            decision="silent", confidence=cached.confidence, reason_code="jev_below_threshold"
        )
        _record_policy(
            journal, state, silenced, reason="jev_below_threshold", decision_source="jev"
        )
        return {"last_participation": silenced, "decision_source": "jev"}
    _record_policy(
        journal, state, cached, reason=_JEV_REASON[cached.decision], decision_source="jev"
    )
    return {"last_participation": cached, "decision_source": "jev"}


def build_participation_subgraph() -> CompiledStateGraph[
    ParticipationState, ParticipationRuntime, ParticipationState, ParticipationState
]:
    def rules(state: ParticipationState) -> dict[str, ParticipationDecision]:
        return {"rule_decision": _rule_as_decision(choose_participation(state["context"]))}

    def decide_rule(
        state: ParticipationState, runtime: Runtime[ParticipationRuntime]
    ) -> ParticipationVerdict:
        rule = state["rule_decision"]
        if rule is None:
            raise RuntimeError("the rule decision runs only after the rules node")
        _record_policy(
            runtime.context.journal, state, rule, reason=rule.reason_code, decision_source="rule"
        )
        return {"last_participation": rule, "decision_source": "rule"}

    def consult_cache(
        state: ParticipationState, runtime: Runtime[ParticipationRuntime]
    ) -> ParticipationVerdict:
        return _verdict_from_cache(state, runtime)

    builder = StateGraph(ParticipationState, context_schema=ParticipationRuntime)
    builder.add_node("rules", rules)
    builder.add_node("decide_rule", decide_rule)
    builder.add_node("consult_cache", consult_cache)
    builder.add_edge(START, "rules")
    builder.add_conditional_edges(
        "rules",
        lambda state: "consult_cache" if _consults_model(state) else "decide_rule",
        {"consult_cache": "consult_cache", "decide_rule": "decide_rule"},
    )
    builder.add_edge("decide_rule", END)
    builder.add_edge("consult_cache", END)
    return builder.compile()
