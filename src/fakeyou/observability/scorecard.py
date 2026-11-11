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
    value = report["latency"].get("speech_stopped_to_first_audio_seconds", {}).get(quantile)
    return float(value) if value is not None else None


def _first_answer_audio(report: Report, quantile: str) -> float | None:
    value = report["latency"].get("speech_stopped_to_first_answer_audio_seconds", {}).get(quantile)
    return float(value) if value is not None else None


def _path_group(report: Report, path: str) -> Mapping[str, Any] | None:
    paths = report.get("latency_by_evidence_path")
    if not isinstance(paths, Mapping):
        return None
    group = paths.get(path)
    return group if isinstance(group, Mapping) else None


def _path_latency(report: Report, path: str, quantile: str, stage: str) -> float | None:
    group = _path_group(report, path)
    if group is None:
        return None
    summary = group.get(stage)
    if not isinstance(summary, Mapping):
        return None
    value = summary.get(quantile)
    return float(value) if value is not None else None


def _reply_rate(report: Report, numerator: str, denominator: str) -> float | None:
    replies = report["replies"]
    count = replies.get(denominator, 0)
    value = replies.get(numerator)
    if not count or value is None:
        return None
    return float(value) / float(count)


def _trailing_question_rate(report: Report) -> float | None:
    replies = report["replies"]
    spoken = replies["spoken"]
    return round(replies["trailing_questions"] / spoken, 3) if spoken else None


def _session_completed(report: Report) -> float | None:
    outcome = report["session_outcome"]
    return None if outcome == "unknown" else float(outcome == "completed")


BASE_TARGETS_VERSION = 2
PATH_GATES = (
    "first_answer_audio_p50_no_visual_seconds",
    "first_answer_audio_p95_no_visual_seconds",
    "first_answer_audio_p50_visual_cached_seconds",
    "first_answer_audio_p95_visual_cached_seconds",
)
PATH_GATE_PATHS = {
    "first_answer_audio_p50_no_visual_seconds": "no_visual",
    "first_answer_audio_p95_no_visual_seconds": "no_visual",
    "first_answer_audio_p50_visual_cached_seconds": "visual_cached",
    "first_answer_audio_p95_visual_cached_seconds": "visual_cached",
}

SCORED_METRICS: dict[str, Callable[[Report], float | None]] = {
    "session_completed": _session_completed,
    "first_audio_p50_seconds": lambda report: _first_audio(report, "p50"),
    "first_audio_p95_seconds": lambda report: _first_audio(report, "p95"),
    "first_answer_audio_p50_seconds": lambda report: _first_answer_audio(report, "p50"),
    "first_answer_audio_p95_seconds": lambda report: _first_answer_audio(report, "p95"),
    "answer_audio_coverage": lambda report: _reply_rate(
        report, "answers_with_audio", "expected_answers"
    ),
    "first_answer_audio_p50_no_visual_seconds": lambda report: _path_latency(
        report, "no_visual", "p50", "first_answer_audio_seconds"
    ),
    "first_answer_audio_p95_no_visual_seconds": lambda report: _path_latency(
        report, "no_visual", "p95", "first_answer_audio_seconds"
    ),
    "first_answer_audio_p50_visual_cached_seconds": lambda report: _path_latency(
        report, "visual_cached", "p50", "first_answer_audio_seconds"
    ),
    "first_answer_audio_p95_visual_cached_seconds": lambda report: _path_latency(
        report, "visual_cached", "p95", "first_answer_audio_seconds"
    ),
    "unnecessary_question_rate": lambda report: _reply_rate(
        report, "unnecessary_questions", "spoken"
    ),
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


def _version(report: Report, targets: SessionTargets) -> int:
    if any(name in PATH_GATES for name in targets.metrics) and not report.get("evidence_paths"):
        return BASE_TARGETS_VERSION
    return targets.version


def _metric_status(name: str, report: Report, value: float | None, *, passed: bool | None) -> str:
    if name in PATH_GATES:
        path = PATH_GATE_PATHS[name]
        group = _path_group(report, path)
        expected = group.get("expected_answers") if isinstance(group, Mapping) else None
        if isinstance(expected, int) and expected == 0:
            return "not_applicable"
        if value is None:
            return "unscored"
    if value is None:
        return "unscored"
    return "passed" if passed else "failed"


def score_session(report: Report, targets: SessionTargets | None = None) -> dict[str, Any]:
    if targets is None:
        targets = SESSION_TARGETS
    metrics: dict[str, dict[str, Any]] = {}
    for name, target in targets.metrics.items():
        if name in PATH_GATES and not report.get("evidence_paths"):
            continue
        value = SCORED_METRICS[name](report)
        passed = None if value is None else target.accepts(value)
        status = _metric_status(name, report, value, passed=passed)
        metrics[name] = {
            **target.model_dump(exclude_none=True),
            "value": value,
            "status": status,
            "passed": passed,
        }
    failed = [name for name, metric in metrics.items() if metric["status"] == "failed"]
    unscored = [name for name, metric in metrics.items() if metric["status"] == "unscored"]
    return {
        "version": _version(report, targets),
        "passed": not failed and not unscored,
        "failed": failed,
        "unscored": unscored,
        "metrics": metrics,
    }
