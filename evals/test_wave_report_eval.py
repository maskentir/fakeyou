import json
from pathlib import Path

from evals._replay import ReplayScore, write_replay_artifacts
from evals._wave_report import GateRow, WaveReportSpec, render_wave_report
from evals.runtime_evidence import EVAL_CANDIDATE_SCHEMA, capture_candidate, finish_candidate
from fakeyou.config.settings import Settings


def test_candidate_schema_is_the_frozen_v1() -> None:
    assert EVAL_CANDIDATE_SCHEMA == "eval_candidate/v1"


def test_wave_report_compares_each_gate_with_the_baseline(tmp_path: Path) -> None:
    score = ReplayScore()
    score.add("answered", passed=True)
    score.latencies.append(0.5)
    score.turns.append({"run_index": 0})
    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"sessions": []}', encoding="utf-8")
    write_replay_artifacts(
        tmp_path,
        score=score,
        settings=Settings(),
        fixture_path=fixture,
        source_commit="wave-commit",
    )
    replay_summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))

    report = render_wave_report(
        WaveReportSpec(
            wave=1,
            integration_commit="wave-commit",
            gates=[
                GateRow(
                    name="suite non-live",
                    command="uv run pytest -m 'not live and not container' -q",
                    result="8611 passed",
                    baseline="8611 passed on d780dca",
                )
            ],
            replay_summary=replay_summary,
            gaps=["C901 still open on Track B"],
            live={
                "fungsi": "not measured",
                "kualitas": "not measured",
                "performa": "not measured",
            },
        )
    )

    assert "wave-commit" in report
    assert "d780dca" in report
    assert "suite non-live" in report
    assert "C901 still open on Track B" in report
    assert report.count("not measured") == 3


def test_candidate_hash_round_trips_on_a_real_tree(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.json"
    fixture.write_text("{}", encoding="utf-8")
    candidate = capture_candidate(fixture, root=tmp_path)

    finished = finish_candidate(candidate, root=tmp_path)

    assert finished["schema"] == EVAL_CANDIDATE_SCHEMA
