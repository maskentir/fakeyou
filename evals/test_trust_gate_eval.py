from pathlib import Path
from typing import Any, cast

import pytest

from evals._shared import contract_reply
from evals._trust import (
    case_owner_model,
    load_trust_fixture,
    passing_trust_reviews,
    summarize_trust,
    trust_gate,
    write_trust_summary,
)
from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.persistence.owner_model import OWNER_MODEL_REF_PREFIX


def test_missing_reviews_and_one_severe_violation_fail_gate() -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = [
        {
            "case_id": case_id,
            "run_index": run,
            "severe": False,
            "useful": True,
            "reason": "Matches supplied evidence",
        }
        for run in range(3)
        for case_id in sorted(case_ids)
    ]
    assert trust_gate(reviews, expected_case_ids=case_ids, runs=3)
    assert not trust_gate(reviews[:-1], expected_case_ids=case_ids, runs=3)
    reviews[17]["severe"] = True
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)


def test_trust_gate_rejects_malformed_reviews() -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    assert not trust_gate(cast(Any, "reviews"), expected_case_ids=case_ids, runs=3)
    assert not trust_gate([], expected_case_ids=set(), runs=3)
    assert not trust_gate(passing_trust_reviews(case_ids, 3), expected_case_ids=case_ids, runs=0)
    assert not trust_gate(passing_trust_reviews(case_ids, 3), expected_case_ids=case_ids, runs=True)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["severe"] = "false"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["useful"] = 1
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["reason"] = "   "
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["run_index"] = 3
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["case_id"] = "c99"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[1] = dict(malformed[0])
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["mode"] = "live"
    assert not trust_gate(malformed, expected_case_ids=case_ids, runs=3)
    malformed = passing_trust_reviews(case_ids, 3)
    malformed[0]["mode"] = "live"
    malformed[0]["source_ref"] = "c01 turn reply plus owner model evidence"
    malformed[0]["checked"] = ["all_turns"]
    assert trust_gate(malformed, expected_case_ids=case_ids, runs=3)


def test_trust_gate_enforces_usefulness_floor() -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    for index in range(8):
        reviews[index]["useful"] = False
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)
    reviews = passing_trust_reviews(case_ids, 3)
    for index in range(7):
        reviews[index]["useful"] = False
    assert trust_gate(reviews, expected_case_ids=case_ids, runs=3)


@pytest.mark.parametrize(
    "reason",
    [
        "Answer cites transcript as source although evidence comes from the owner model.",
        "Answer presents source date as event date.",
        "Answer greets a name without identity source.",
        "Answer implicitly approves a contract without written permission.",
        "Answer is empty without any clarification.",
        "Answer presents unavailable status as known fact.",
    ],
)
def test_severe_violation_fails_gate(reason: str) -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    reviews[5]["severe"] = True
    reviews[5]["reason"] = reason
    assert not trust_gate(reviews, expected_case_ids=case_ids, runs=3)


def test_missing_single_review_fails_gate() -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    assert not trust_gate(reviews[1:], expected_case_ids=case_ids, runs=3)


def test_trust_corpus_defines_29_rated_cases() -> None:
    fixture = load_trust_fixture()
    cases = fixture["cases"]
    assert [case["id"] for case in cases] == [f"c{i:02d}" for i in range(1, 30)]
    families: dict[str, int] = {}
    for case in cases:
        families[case["family"]] = families.get(case["family"], 0) + 1
        assert case["expect"] in {"answer", "unknown", "clarify", "refuse"}
        assert isinstance(case["needs_recall"], bool)
        assert case["rubric"].strip()
        assert case["turns"]
        for turn in case["turns"]:
            assert turn["say"].strip()
            assert contract_reply(turn).strip()
            assert cap_spoken_reply(contract_reply(turn)) == contract_reply(turn).strip()
    assert families == {"sumber_waktu": 11, "koreksi_gagal": 8, "otoritas_guna": 10}


def test_trust_owner_model_cases_hold_approved_facts_and_cite_them() -> None:
    fixture = load_trust_fixture()
    cases = fixture["cases"]
    owner_cases = {case["id"] for case in cases if "owner_model" in case}
    assert owner_cases == {f"c{i:02d}" for i in (1, 2, 3, 9, 10, 11, 12, 13, 14, 15, 16, 24)}
    assert {item["kind"] for item in fixture["owner_model"]} == {"fact", "preference"}
    for case in cases:
        stored = case.get("memory", {}).get("stored", {}).values()
        assert all(item["label"] != "owner note" for item in stored)
        owner_model = case_owner_model(fixture, case)
        refs = {item.ref for item in owner_model.items}
        assert len(refs) == len(owner_model.items)
        for item in owner_model.items:
            assert item.confidence == "firm"
            assert item.disclosure == "room_speakable"
            assert item.statement.strip()
        for item in case.get("owner_model", []):
            assert item["kind"] == "fact"
            assert item["valid_at"]
        for turn in case["turns"]:
            cited = {str(ref) for claim in turn.get("contract_claims", []) for ref in claim["refs"]}
            assert {ref for ref in cited if ref.startswith(OWNER_MODEL_REF_PREFIX)} <= refs


def test_trust_summary_reports_gate_and_usefulness(tmp_path: Path) -> None:
    case_ids = {f"c{i:02d}" for i in range(1, 26)}
    reviews = passing_trust_reviews(case_ids, 3)
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == 1.0
    assert summary["contract_only"] is True
    reviews[0]["useful"] = False
    summary = summarize_trust(reviews, expected_case_ids=case_ids, runs=3)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == round(74 / 75, 3)
    write_trust_summary(tmp_path, summary)
    assert (tmp_path / "summary.json").exists()


@pytest.mark.parametrize("marker", [{"skipped": True}, {"verdict": "skip"}, {"mode": []}])
def test_trust_gate_rejects_skips_and_malformed_mode(marker: dict[str, Any]) -> None:
    reviews = passing_trust_reviews({"c01"}, 1)
    reviews[0].update(marker)
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)


def test_live_trust_review_requires_all_turns_attestation() -> None:
    reviews = passing_trust_reviews({"c01"}, 1)
    reviews[0].update(mode="live", source_ref="turns.json c01 all replies")
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)
    reviews[0]["checked"] = ["all_turns"]
    assert trust_gate(reviews, expected_case_ids={"c01"}, runs=1)
    reviews[0]["severe"] = True
    reviews[0]["reason"] = "Unrated first response contains unsupported approval."
    assert not trust_gate(reviews, expected_case_ids={"c01"}, runs=1)
