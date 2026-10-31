from dataclasses import dataclass, replace
from typing import Any

from fakeyou.observability.baseline import baseline_metrics

LATENCY_TOLERANCE = 0.1
COUNT_TOLERANCE = 0.1
COUNT_MINIMUM = 1.0

LATENCY_METRICS = (
    "vad_stop_to_final_ms",
    "final_to_first_pcm_ms",
    "final_to_first_audio_ms",
    "vad_stop_to_first_audio_ms",
    "barge_in_to_silence_ms",
    "sentence_gap_ms",
)

REPORTED_COUNTS = {
    "barge_in_rejected": "interim text is not recorded, so interims are synthesized",
    "early_turn_started": "interim text is not recorded, so interims are synthesized",
    "screen_reads": "the vision model is not replayed",
    "screen_duplicates": "the vision model is not replayed",
    "memory_prefetches": "memory is not replayed",
}

NO_RECORDING = "the recorded session has no data for this metric"
INPUT_LATENCIES = frozenset({"vad_stop_to_final_ms.p50"})


@dataclass(frozen=True, slots=True)
class MetricCheck:
    name: str
    original: Any
    replay: Any
    difference: float | None
    tolerance: float | None
    passed: bool
    gated: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Comparison:
    checks: tuple[MetricCheck, ...]

    @property
    def comparable(self) -> bool:
        return any(
            check.name.endswith(".p50") and check.name not in INPUT_LATENCIES
            for check in self.gated()
        )

    @property
    def passed(self) -> bool:
        return self.comparable and all(check.passed for check in self.gated())

    @property
    def failures(self) -> tuple[MetricCheck, ...]:
        return tuple(check for check in self.gated() if not check.passed)

    def gated(self) -> tuple[MetricCheck, ...]:
        return tuple(check for check in self.checks if check.gated)

    def reported(self) -> tuple[MetricCheck, ...]:
        return tuple(check for check in self.checks if not check.gated)


def compare_journals(original: list[dict[str, Any]], replay: list[dict[str, Any]]) -> Comparison:
    return compare_metrics(baseline_metrics(original), baseline_metrics(replay))


def compare_metrics(original: dict[str, Any], replay: dict[str, Any]) -> Comparison:
    checks: list[MetricCheck] = []
    for name in LATENCY_METRICS:
        checks.extend(_latency(name, original.get(name), replay.get(name)))
    for name, reason in REPORTED_COUNTS.items():
        checks.append(
            _reported(
                _value(name, original.get(name), replay.get(name), COUNT_TOLERANCE, COUNT_MINIMUM),
                reason,
            )
        )
    checks.extend(_speaker_bound(original.get("speaker_bound"), replay.get("speaker_bound")))
    checks.extend(
        _reported(check, "replies come from the recorded model")
        for check in _share(
            "contributions_spoken",
            original.get("contributions_spoken"),
            replay.get("contributions_spoken"),
        )
    )
    return Comparison(tuple(checks))


def _latency(name: str, original: Any, replay: Any) -> list[MetricCheck]:
    left = original if isinstance(original, dict) else {}
    right = replay if isinstance(replay, dict) else {}
    checks = [
        _value(
            f"{name}.count", left.get("count"), right.get("count"), COUNT_TOLERANCE, COUNT_MINIMUM
        ),
        _value(f"{name}.p50", left.get("p50"), right.get("p50"), LATENCY_TOLERANCE),
    ]
    if not left.get("count"):
        checks = [_reported(check, NO_RECORDING) for check in checks]
    checks.append(
        _reported(
            MetricCheck(
                f"{name}.p95",
                _number(left.get("p95")),
                _number(right.get("p95")),
                None,
                None,
                passed=True,
                gated=False,
            ),
            "p95 rests on too few turns to gate",
        )
    )
    return checks


def _speaker_bound(original: Any, replay: Any) -> list[MetricCheck]:
    left = original if isinstance(original, dict) else {}
    right = replay if isinstance(replay, dict) else {}
    of = MetricCheck(
        "speaker_bound.of",
        left.get("of"),
        right.get("of"),
        None,
        None,
        passed=left.get("of") == right.get("of"),
        gated=True,
        reason="every final is a replayed input",
    )
    count = _share("speaker_bound", original, replay)[1]
    return [of, _reported(count, "speakers are not bound in replay")]


def _share(name: str, original: Any, replay: Any) -> list[MetricCheck]:
    left = original if isinstance(original, dict) else {}
    right = replay if isinstance(replay, dict) else {}
    return [
        _value(f"{name}.of", left.get("of"), right.get("of"), COUNT_TOLERANCE, COUNT_MINIMUM),
        _value(
            f"{name}.count", left.get("count"), right.get("count"), COUNT_TOLERANCE, COUNT_MINIMUM
        ),
    ]


def _reported(check: MetricCheck, reason: str) -> MetricCheck:
    return replace(check, gated=False, reason=reason)


def _value(
    name: str, original: Any, replay: Any, tolerance: float, minimum: float = 0.0
) -> MetricCheck:
    left = _number(original)
    right = _number(replay)
    if left is None or right is None:
        passed = left is None and right is None
        return MetricCheck(name, left, right, None, tolerance, passed=passed, gated=True)
    difference = abs(left - right)
    allowed = max(tolerance * abs(left), minimum)
    return MetricCheck(
        name, left, right, difference, allowed, passed=difference <= allowed, gated=True
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
