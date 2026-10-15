from pathlib import Path

from evals._participation_replay import load_labels, replay_record, score_replay

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "participation-record-trust-live-s1.jsonl"


def test_replay_produces_one_decision_per_recorded_turn() -> None:
    decisions = replay_record(FIXTURE, twin_names=("rani twin",))
    assert len(decisions) == 13
    assert {decision["action"] for decision in decisions} <= {"reply", "silent", "clarify"}


def test_replay_agreement_reports_gap_against_human_labels() -> None:
    labels = load_labels("trust-live-s1")
    decisions = replay_record(FIXTURE, twin_names=tuple(labels["twin_names"]))
    result = score_replay(decisions, labels["turns"])
    assert result["turns"] == 13
    assert 0.0 <= result["agreement"] <= 1.0
    assert result["misses"]
