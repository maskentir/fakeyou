import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from fakeyou.foundation.claims import (
    Claim,
    claim_languages,
    markers_for,
    parse_claims,
    support_claim,
    verify_claim,
)
from fakeyou.foundation.gathered_evidence import GatheredEvidence
from tests.test_publication_freshness import evidence as screen_evidence
from tests.test_publication_identity import camera_evidence

CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "claim-support-cases.json").read_text()
)["cases"]
REVENUE = Claim(
    kind="screen",
    shape="entity_attribute_value",
    entity="Project A",
    attribute="revenue",
    value="$1.2M",
)


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_labelled_support_case(case: dict[str, Any]) -> None:
    claim = Claim.model_validate(case["claim"])

    support = support_claim(claim, case["answer"], status=case["status"], language=case["language"])

    assert support.reason == case["expected"]
    assert support.supported is (case["expected"] == "ok")


@pytest.mark.parametrize(
    "case", [case for case in CASES if case["expected"] == "ok"], ids=lambda case: case["id"]
)
def test_supported_claim_reports_spans_of_the_answer_inside_one_clause(
    case: dict[str, Any],
) -> None:
    claim = Claim.model_validate(case["claim"])
    answer = case["answer"]

    support = support_claim(claim, answer, status="produced", language=case["language"])

    assert support.clause is not None
    for name, span in support.spans.items():
        assert answer[span.start : span.end] == span.text
        assert support.clause.start <= span.start <= span.end <= support.clause.end
        declared = str(case["claim"][name])
        assert span.text.casefold().split() == declared.casefold().split()


def test_both_languages_are_covered_by_labelled_cases_of_every_reason() -> None:
    assert claim_languages() == {"en", "id"}
    for language in ("en", "id"):
        reasons = {case["expected"] for case in CASES if case["language"] == language}
        assert reasons >= {
            "ok",
            "claim_relation_unsupported",
            "claim_untraceable",
            "claim_polarity_unsupported",
            "claim_relation_ambiguous",
            "claim_incomplete",
            "validation_support_unknown",
        }


def test_unknown_language_uses_the_english_markers() -> None:
    assert markers_for("fr") == markers_for("en")
    assert markers_for(None) == markers_for("en")
    assert markers_for("id") != markers_for("en")


def test_proposal_is_parsed_from_the_marked_block_of_the_reply() -> None:
    reply = (
        "Revenue is $1.2M and growing fast. CLAIMS: "
        '[{"kind": "screen", "shape": "entity_attribute_value", "entity": "Project A", '
        '"attribute": "revenue", "value": "$1.2M", "dependence": "content"}] thanks'
    )

    [claim] = parse_claims(reply)

    assert claim == replace_dependence(REVENUE, "content")


def replace_dependence(claim: Claim, dependence: str) -> Claim:
    return claim.model_copy(update={"dependence": dependence})


@pytest.mark.parametrize(
    "reply",
    [
        "Revenue is $1.2M.",
        "CLAIMS: not json",
        'CLAIMS: {"kind": "screen"}',
        'CLAIMS: [{"kind": "screen", "shape": "item_presence", "item": "x", "scope": "no_visual"}]',
        'CLAIMS: [{"kind": "chat", "shape": "item_presence", "item": "x"}]',
        'CLAIMS: [{"kind": "screen", "shape": "trend", "item": "x"}]',
        'CLAIMS: [{"kind": "screen", "shape": "item_presence", "item": 7}]',
    ],
)
def test_malformed_or_extended_proposal_yields_no_claims(reply: str) -> None:
    assert parse_claims(reply) == ()


def gathered(*items: Any, answer: str = "Revenue for Project A is $1.2M.") -> GatheredEvidence:
    return GatheredEvidence(
        {item.kind: {"code": "ok", "answer": answer} for item in items}, "gather", tuple(items)
    )


def test_claim_is_verified_against_the_look_of_its_own_kind() -> None:
    look = screen_evidence()

    verified = verify_claim(REVENUE, gathered(look), language="en")

    assert verified.supported
    assert verified.evidence is look
    assert verified.answer == "Revenue for Project A is $1.2M."


def test_claim_without_a_look_of_its_kind_is_missing_evidence() -> None:
    camera = camera_evidence()

    assert verify_claim(REVENUE, None, language="en").support.reason == "claim_evidence_missing"
    verified = verify_claim(REVENUE, gathered(camera), language="en")
    assert (verified.supported, verified.support.reason) == (False, "claim_evidence_missing")


def test_claim_declared_against_evidence_of_another_source_is_a_mismatch() -> None:
    mislabelled = replace(camera_evidence(), kind="screen")

    verified = verify_claim(REVENUE, gathered(mislabelled), language="en")

    assert (verified.supported, verified.support.reason) == (False, "claim_evidence_mismatch")


def test_claim_against_a_look_that_did_not_produce_is_not_supported() -> None:
    look = replace(screen_evidence(), status="insufficient")

    verified = verify_claim(REVENUE, gathered(look), language="en")

    assert verified.support.reason == "validation_support_unknown"


def test_claim_against_a_payload_without_an_answer_is_not_supported() -> None:
    look = screen_evidence()
    evidence = GatheredEvidence({"screen": {"code": "ok"}}, "gather", (look,))

    assert verify_claim(REVENUE, evidence, language="en").support.reason == (
        "validation_support_unknown"
    )
