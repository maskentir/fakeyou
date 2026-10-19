from collections.abc import Mapping
from typing import Any

from evals._replay import (
    MAX_TRAILING_QUESTION_RATE,
    REPLY_LATENCY_BUDGET_SECONDS,
    REQUIRED_PASS_RATE,
)

FROZEN_THRESHOLDS = {
    "required_pass_rate": REQUIRED_PASS_RATE,
    "max_trailing_question_rate": MAX_TRAILING_QUESTION_RATE,
    "reply_latency_budget_seconds": REPLY_LATENCY_BUDGET_SECONDS,
}


def gate_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    if payload.get("thresholds") != FROZEN_THRESHOLDS:
        reasons.append("thresholds differ from the frozen replay budgets")
    if not payload.get("turns"):
        reasons.append("candidate artifact carries no replay turns")
    gates = payload.get("gates")
    if not isinstance(gates, Mapping) or not gates:
        reasons.append("candidate artifact carries no gates")
    else:
        for name in sorted(gates):
            gate = gates[name]
            if not isinstance(gate, Mapping) or gate.get("passed") is not True:
                reasons.append(f"gate failed: {name}")
    return {"passed": not reasons, "reasons": reasons}
