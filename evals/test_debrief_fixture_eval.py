import hashlib
import json
from typing import Any

import pytest

from evals._debrief_support import (
    capture_debrief_candidate,
    contract_reviews,
    debrief_gate,
    fixture_context_text,
    load_debrief_fixture,
    record_bytes,
)


def contract_results(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for case in fixture["cases"]:
        raw = record_bytes(case)
        report = None
        meeting_id = f"debrief-eval-{case['id']}-0"
        context_json = fixture_context_text(fixture, case, meeting_id)
        if case.get("expect") != "preprovider_failure":
            entries = {
                f"transcript:{entry['id']}": entry
                for entry in case["entries"]
                if entry["kind"] == "transcript"
            }
            items = [
                dict(
                    kind=criterion["kind"],
                    text=criterion["meaning"],
                    status=criterion["statuses"][0],
                    evidence=criterion["evidence"],
                    assignee_claim=criterion.get("assignee_claim"),
                    due_text=criterion.get("due_text"),
                )
                for criterion in case["required_items"]
            ]
            cited = {ref["entry_id"] for item in items for ref in item["evidence"]}
            sources = [
                dict(
                    id=source_id,
                    text=entries[source_id]["text"],
                    kind="participant",
                    participant_id=entries[source_id]["participant_id"],
                    at=entries[source_id]["at"],
                )
                for source_id in sorted(cited)
            ]
            report = dict(
                schema_version="meeting_debrief/v1",
                owner_id=fixture["persona"]["owner_id"],
                twin_id=fixture["persona"]["twin_id"],
                meeting_id=meeting_id,
                record_sha256=hashlib.sha256(raw).hexdigest(),
                context_sha256=hashlib.sha256(context_json.encode()).hexdigest()
                if context_json
                else None,
                generated_at="2026-10-03T03:00:00+00:00",
                provider="openai",
                model="offline-contract",
                source_count=len(
                    [
                        entry
                        for entry in case["entries"]
                        if entry["kind"] in ("transcript", "twin_speech")
                    ]
                ),
                chunk_count=2 if case["category"] == "chunk_boundary" else 1,
                status="completed",
                review_status="unreviewed",
                items=items,
                findings=[
                    dict(objective_id=criterion["id"], status=criterion["statuses"][0], evidence=[])
                    for criterion in case.get("required_findings", [])
                ],
                evidence_sources=sources,
                source_limitations=["Independent semantic review is required."],
            )
        report_json = json.dumps(report) if report else None
        results.append(
            dict(
                case_id=case["id"],
                run=0,
                meeting_id=meeting_id,
                context_json=context_json,
                status="completed" if report else "expected_failure",
                error=None if report else "DebriefValidationError",
                model_calls=report["chunk_count"] if report else 0,
                record_sha256=hashlib.sha256(raw).hexdigest(),
                raw_record=raw.decode(),
                report_json=report_json,
                report_sha256=hashlib.sha256(report_json.encode()).hexdigest()
                if report_json
                else None,
            )
        )
    return results


def test_candidate_hashes_include_b_runtime_and_fixture() -> None:
    candidate = capture_debrief_candidate()
    assert "src/fakeyou/debrief/debrief.py" in candidate.get("start_files", {})
    assert "evals/fixtures/debrief-scenarios.json" in candidate.get("start_files", {})


def test_fixture_has_eight_exact_categories_and_no_completed_truncated_twin() -> None:
    fixture = load_debrief_fixture()
    assert len(fixture["cases"]) == 8
    assert {case["category"] for case in fixture["cases"]} == {
        "conflict",
        "unknown",
        "truncated_twin",
        "false_instructions",
        "owner_decision",
        "relative_time",
        "chunk_boundary",
        "missing_corrupt_source",
    }
    truncated = fixture["cases"][2]
    assert not any(entry["kind"] == "twin_speech" for entry in truncated["entries"])


def test_ground_truth_review_is_required_even_for_valid_json() -> None:
    assert not debrief_gate([], [], load_debrief_fixture())


def test_candidate_is_bound_to_actual_source_bytes() -> None:
    candidate = capture_debrief_candidate()
    assert len(candidate.get("start_sha256", "")) == 64


def test_reviewed_reports_pass_only_after_each_item_and_criterion_is_checked() -> None:
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    assert debrief_gate(reviews, results, fixture)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "skipped",
        "unchecked",
        "fabricated",
        "severe",
        "uncovered",
        "wrong_metadata",
    ],
)
def test_review_gate_rejects_invalid_semantic_or_source_evidence(mutation: str) -> None:
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    if mutation == "missing":
        reviews.pop()
    elif mutation == "duplicate":
        reviews.append(reviews[0])
    elif mutation == "skipped":
        reviews[0]["verdict"] = "skipped"
    elif mutation == "unchecked":
        reviews[0]["checked"].remove("all_items")
    elif mutation == "fabricated":
        payload = json.loads(results[0]["report_json"].replace("transcript:c1", "transcript:fake"))
        _replace_report_for_contract(results, reviews, 0, payload)
    elif mutation == "severe":
        reviews[0]["severe_violations"] = ["unsupported_owner_approval"]
    elif mutation == "uncovered":
        reviews[0]["coverage"] = []
    elif mutation == "wrong_metadata":
        payload = json.loads(results[1]["report_json"].replace("unknown", "the-owner"))
        _replace_report_for_contract(results, reviews, 1, payload)
    assert not debrief_gate(reviews, results, fixture)


def _mutate_review_items(reviews: list[dict[str, Any]], mutation: str) -> bool:
    if mutation == "malformed":
        reviews[0]["independent"] = "yes"
    elif mutation == "duplicate_item_review":
        reviews[0]["all_items"].append(reviews[0]["all_items"][0])
    elif mutation == "missing_item_review":
        reviews[0]["all_items"].pop()
    elif mutation == "missing_finding_review":
        reviews[4]["all_findings"].pop()
    elif mutation == "unsupported":
        reviews[0]["all_items"][0]["supported"] = False
    elif mutation == "false_approval":
        reviews[1]["owner_approval_supported"] = False
    else:
        return False
    return True


def _mutate_review(
    reviews: list[dict[str, Any]], results: list[dict[str, Any]], mutation: str
) -> None:
    if _mutate_review_items(reviews, mutation):
        return
    if mutation == "unknown_identity":
        reviews[1]["unknown_preserved"] = False
    elif mutation == "external_action":
        reviews[4]["no_external_action"] = False
    elif mutation == "bad_criterion":
        reviews[0]["coverage"][0]["criterion_id"] = "invented"
    elif mutation == "wrong_item_mapping":
        reviews[0]["coverage"][0]["item_indices"] = [1]
    elif mutation == "wrong_quote":
        payload = json.loads(results[0]["report_json"])
        payload["items"][0]["evidence"][0]["quote"] = "not present in the source"
        _replace_report_for_contract(results, reviews, 0, payload)
    elif mutation == "empty_rationale":
        reviews[0]["all_items"][0]["rationale"] = " "


@pytest.mark.parametrize(
    "mutation",
    [
        "malformed",
        "duplicate_item_review",
        "missing_item_review",
        "missing_finding_review",
        "unsupported",
        "false_approval",
        "unknown_identity",
        "external_action",
        "bad_criterion",
        "wrong_item_mapping",
        "wrong_quote",
        "empty_rationale",
    ],
)
def test_gate_rejects_review_and_coverage_contract_failures(mutation: str) -> None:
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    _mutate_review(reviews, results, mutation)
    assert not debrief_gate(reviews, results, fixture)


def _replace_report_for_contract(
    results: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    index: int,
    payload: dict[str, Any],
) -> None:
    raw = json.dumps(payload)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    results[index].update(report_json=raw, report_sha256=digest)
    reviews[index]["report_sha256"] = digest


def test_required_item_coverage_uses_ground_truth_denominator() -> None:
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    reviews[0]["coverage"].pop()
    assert debrief_gate(reviews, results, fixture)
    reviews[0]["coverage"].pop()
    assert not debrief_gate(reviews, results, fixture)


def test_offline_contract_reviews_cannot_approve_live_artifacts() -> None:
    fixture = load_debrief_fixture()
    results = contract_results(fixture)
    reviews = contract_reviews(fixture, results)
    assert not debrief_gate(reviews, results, fixture, mode="live")
