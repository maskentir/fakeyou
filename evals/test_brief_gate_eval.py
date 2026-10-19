import json
from pathlib import Path
from typing import Any, cast

import pytest

from evals._brief import (
    BRIEF_RUNS,
    EXPECTED_CASE_IDS,
    MIN_USEFUL_OF_36,
    brief_gate,
    passing_brief_reviews,
    summarize_briefs,
)
from evals._trust import write_trust_summary


def test_missing_reviews_and_one_severe_violation_fail_brief_gate() -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    assert len(reviews) == 36
    assert brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    assert not brief_gate(reviews[:-1], expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    assert not brief_gate(reviews[1:], expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    reviews[17]["severe"] = True
    reviews[17]["reason"] = "Reply approves a new deadline without owner authority."
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_gate_rejects_duplicate_pairs() -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    reviews[1] = dict(reviews[0])
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    reviews.append(dict(reviews[0]))
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


@pytest.mark.parametrize(
    "marker",
    [
        {"skipped": True},
        {"skipped": "yes"},
        {"skipped": None},
        {"verdict": "skip"},
        {"verdict": " Skipped "},
    ],
)
def test_brief_gate_rejects_skipped_reviews(marker: dict[str, object]) -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    reviews[4].update(marker)
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_gate_accepts_explicitly_unskipped_reviews() -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    reviews[4]["skipped"] = False
    reviews[5]["verdict"] = "pass"
    assert brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_gate_enforces_usefulness_floor_at_33_of_36() -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    for index in range(4):
        reviews[index]["useful"] = False
    assert sum(review["useful"] is True for review in reviews) == 32
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    for index in range(3):
        reviews[index]["useful"] = False
    assert sum(review["useful"] is True for review in reviews) == MIN_USEFUL_OF_36
    assert brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_gate_rejects_malformed_reviews() -> None:
    case_ids = EXPECTED_CASE_IDS
    assert not brief_gate(cast(Any, "reviews"), expected_case_ids=case_ids, runs=BRIEF_RUNS)
    assert not brief_gate([], expected_case_ids=set(), runs=BRIEF_RUNS)
    passing = passing_brief_reviews(case_ids, BRIEF_RUNS)
    assert not brief_gate(passing, expected_case_ids=case_ids, runs=0)
    assert not brief_gate(passing, expected_case_ids=case_ids, runs=True)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    cast(list[Any], malformed)[0] = "b01 looks fine"
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["severe"] = "false"
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["useful"] = 1
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["reason"] = "   "
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["run_index"] = BRIEF_RUNS
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["run_index"] = False
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["case_id"] = "b99"
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed = passing_brief_reviews(case_ids, BRIEF_RUNS)
    malformed[0]["mode"] = "live"
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed[0]["source_ref"] = "  "
    assert not brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)
    malformed[0]["source_ref"] = "turns.json b01 run0 plus prompts.json b01"
    assert brief_gate(malformed, expected_case_ids=case_ids, runs=BRIEF_RUNS)


@pytest.mark.parametrize(
    "checked",
    [
        None,
        "reply,prompt_scope,tool_trajectory",
        [],
        ["reply", "prompt_scope"],
        ["reply", "tool_trajectory"],
        ["prompt_scope", "tool_trajectory"],
        [{"reply": True}, "prompt_scope", "tool_trajectory"],
    ],
)
def test_brief_gate_requires_reviewer_to_check_reply_prompt_and_tools(checked: object) -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    if checked is None:
        del reviews[9]["checked"]
    else:
        reviews[9]["checked"] = checked
    assert not brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_gate_accepts_extra_checked_evidence() -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    reviews[9]["checked"] = ["tool_trajectory", "reply", "prompt_scope", "all_turns", "manifest"]
    assert brief_gate(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)


def test_brief_summary_reports_gate_usefulness_and_skips(tmp_path: Path) -> None:
    reviews = passing_brief_reviews(EXPECTED_CASE_IDS, BRIEF_RUNS)
    summary = summarize_briefs(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    assert summary["gate_passed"] is True
    assert summary["usefulness"] == 1.0
    assert summary["pairs_complete"] is True
    assert summary["contract_only"] is True
    reviews[0]["useful"] = False
    reviews[1]["verdict"] = "skip"
    del reviews[2]["checked"]
    summary = summarize_briefs(reviews, expected_case_ids=EXPECTED_CASE_IDS, runs=BRIEF_RUNS)
    assert summary["gate_passed"] is False
    assert summary["useful_count"] == 35
    assert summary["skipped_count"] == 1
    assert summary["incomplete_checks"] == 1
    write_trust_summary(tmp_path, summary)
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == summary


def test_live_brief_review_requires_all_turns_attestation() -> None:
    reviews = passing_brief_reviews({"b01"}, 1)
    reviews[0].update(mode="live", source_ref="turns.json b01 all replies")
    reviews[0]["checked"] = ["reply", "prompt_scope", "tool_trajectory"]
    assert not brief_gate(reviews, expected_case_ids={"b01"}, runs=1)
    checked = reviews[0]["checked"]
    assert isinstance(checked, list)
    checked.append("all_turns")
    assert brief_gate(reviews, expected_case_ids={"b01"}, runs=1)


def test_brief_gate_rejects_malformed_mode_without_raising() -> None:
    reviews = passing_brief_reviews({"b01"}, 1)
    reviews[0]["mode"] = []
    assert not brief_gate(reviews, expected_case_ids={"b01"}, runs=1)
