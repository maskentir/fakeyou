from collections.abc import Callable, Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, model_validator

from fakeyou.foundation.resources import read_package_text

SECONDS_PER_HOUR = 3600.0

Report = Mapping[str, Any]


def _per_hour(count: float, report: Report) -> float | None:
    seconds = report.get("in_call_seconds")
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return round(count * SECONDS_PER_HOUR / seconds, 3)


def _first_audio(report: Report, quantile: str) -> float | None:
    value = report["latency"]["speech_stopped_to_first_pcm_seconds"][quantile]
    return float(value) if value is not None else None


def _trailing_question_rate(report: Report) -> float | None:
    replies = report["replies"]
    spoken = replies["spoken"]
    return round(replies["trailing_questions"] / spoken, 3) if spoken else None


def _session_completed(report: Report) -> float | None:
    outcome = report["session_outcome"]
    return None if outcome == "unknown" else float(outcome == "completed")


SCORED_METRICS: dict[str, Callable[[Report], float | None]] = {
    "session_completed": _session_completed,
    "first_audio_p50_seconds": lambda report: _first_audio(report, "p50"),
    "first_audio_p95_seconds": lambda report: _first_audio(report, "p95"),
    "trailing_question_rate": _trailing_question_rate,
    "faults_per_hour": lambda report: _per_hour(
        sum(fault["count"] for fault in report["faults"]), report
    ),
    "watchdog_failures_per_hour": lambda report: _per_hour(
        report["browser"]["watchdog_failures"], report
    ),
    "attribution_coverage": lambda report: report["participation"]["attribution_coverage"],
    "admission_to_listening_seconds": lambda report: report["startup"][
        "admission_to_listening_seconds"
    ],
}


class MetricTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min: float | None = None
    max: float | None = None

    def accepts(self, value: float) -> bool:
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


class SessionTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    metrics: dict[str, MetricTarget]

    @model_validator(mode="after")
    def metrics_are_scored(self) -> Self:
        unknown = sorted(set(self.metrics) - set(SCORED_METRICS))
        if unknown:
            raise ValueError(f"targets name unscored metrics: {', '.join(unknown)}")
        return self


SESSION_TARGETS = SessionTargets.model_validate_json(read_package_text("session_targets.json"))


def score_session(report: Report, targets: SessionTargets = SESSION_TARGETS) -> dict[str, Any]:
    metrics: dict[str, dict[str, Any]] = {}
    for name, target in targets.metrics.items():
        value = SCORED_METRICS[name](report)
        passed = None if value is None else target.accepts(value)
        metrics[name] = {**target.model_dump(exclude_none=True), "value": value, "passed": passed}
    failed = [name for name, metric in metrics.items() if metric["passed"] is False]
    unscored = [name for name, metric in metrics.items() if metric["passed"] is None]
    return {
        "version": targets.version,
        "passed": not failed and not unscored,
        "failed": failed,
        "unscored": unscored,
        "metrics": metrics,
    }
