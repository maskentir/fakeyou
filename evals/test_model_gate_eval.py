import json
from pathlib import Path
from typing import Any, cast

from evals._model_gate import gate_candidate
from evals._replay import ReplayScore, write_replay_artifacts
from fakeyou.config.settings import Settings


def _passing_payload(tmp_path: Path) -> dict[str, Any]:
    score = ReplayScore()
    for name in (
        "answered",
        "must_mention",
        "must_mention_any",
        "must_not_mention",
        "within_two_sentences",
        "no_trailing_question",
    ):
        score.add(name, passed=True)
    score.latencies.append(0.5)
    score.turns.append({"run_index": 0})
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"sessions": []}', encoding="utf-8")
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=Settings(),
        fixture_path=fixture,
        source_commit="test-commit",
    )
    return cast(dict[str, Any], json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")))


def test_candidate_with_frozen_thresholds_and_passing_gates_passes(tmp_path: Path) -> None:
    verdict = gate_candidate(_passing_payload(tmp_path))

    assert verdict == {"passed": True, "reasons": []}


def test_candidate_with_loosened_thresholds_is_rejected(tmp_path: Path) -> None:
    payload = _passing_payload(tmp_path)
    payload["thresholds"]["required_pass_rate"] = 0.5

    verdict = gate_candidate(payload)

    assert verdict["passed"] is False
    assert verdict["reasons"] == ["thresholds differ from the frozen replay budgets"]


def test_candidate_with_a_failing_gate_names_the_gate(tmp_path: Path) -> None:
    payload = _passing_payload(tmp_path)
    payload["gates"]["latency_p95_s"] = {"value": 9.5, "samples": 1, "passed": False}

    verdict = gate_candidate(payload)

    assert verdict["passed"] is False
    assert verdict["reasons"] == ["gate failed: latency_p95_s"]
