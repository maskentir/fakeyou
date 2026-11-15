import json
import unicodedata
from dataclasses import replace
from importlib.resources import files
from pathlib import Path
from string import Formatter
from typing import Any

import pytest

from fakeyou.foundation.claims import Claim, Span, Support, VerifiedClaim, support_claim
from fakeyou.foundation.rendering import (
    MAX_FIELD_CODE_POINTS,
    MAX_FIELD_WORDS,
    render,
)
from fakeyou.foundation.statement import (
    NOT_RENDERED,
    Tense,
    pattern_text,
    patterns,
    rendering_refusal,
    rerender,
    statement_digest,
)
from tests.test_publication_freshness import evidence as screen_evidence
from tests.test_publication_identity import camera_evidence

CATALOG = json.loads(files("fakeyou").joinpath("assets", "publication_catalog.json").read_text())
MARKERS = json.loads(files("fakeyou").joinpath("assets", "claim_language.json").read_text())
CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "claim-support-cases.json").read_text()
)["cases"]
SUPPORTED = [case for case in CASES if case["expected"] == "ok" and "polarity" not in case["claim"]]
TENSES: tuple[Tense, ...] = ("current", "historical")
ENGLISH = {
    ("current", "entity_attribute_value"): (
        "The currently displayed screen lists {attribute} for {entity} as {value}."
    ),
    ("current", "entity_status"): (
        "The currently displayed screen lists the status of {entity} as {status}."
    ),
    ("current", "item_presence"): "The currently displayed screen shows {item}.",
    ("historical", "entity_attribute_value"): (
        "The checked frame listed {attribute} for {entity} as {value}."
    ),
    ("historical", "entity_status"): "The checked frame listed the status of {entity} as {status}.",
    ("historical", "item_presence"): "The checked frame showed {item}.",
}


def verified_case(case: dict[str, Any]) -> VerifiedClaim:
    claim = Claim.model_validate(case["claim"])
    look = screen_evidence() if claim.kind == "screen" else camera_evidence()
    support = support_claim(claim, case["answer"], status="produced", language=case["language"])
    return VerifiedClaim(claim, look, support, case["answer"])


def located(answer: str, claim: Claim, **fields: str) -> VerifiedClaim:
    spans = {
        name: Span(answer.index(text), answer.index(text) + len(text), text)
        for name, text in fields.items()
    }
    return VerifiedClaim(claim, screen_evidence(), Support("ok", spans), answer)


REVENUE = Claim(
    kind="screen",
    shape="entity_attribute_value",
    entity="Project A",
    attribute="revenue",
    value="$1.2M",
)
ANSWER = "Revenue for Project A is $1.2M."


def revenue(answer: str = ANSWER, **fields: str) -> VerifiedClaim:
    return located(
        answer,
        REVENUE,
        **{"entity": "Project A", "attribute": "Revenue", "value": "$1.2M", **fields},
    )


def test_catalog_holds_the_approved_patterns_in_both_languages() -> None:
    version, known = patterns()

    assert version == 1
    assert len(known) == 12
    for (tense, shape), text in ENGLISH.items():
        assert pattern_text(tense, shape, "en") == text
        indonesian = pattern_text(tense, shape, "id")
        assert indonesian == CATALOG["patterns"][tense][shape]["id"]
        assert indonesian is not None
        assert indonesian != text
        assert placeholders(indonesian) == placeholders(text)


def placeholders(pattern: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(pattern) if name}


def test_entity_attribute_value_is_rendered_in_the_approved_order() -> None:
    current = render(revenue(), "current", "en")
    historical = render(revenue(), "historical", "en")

    assert current is not None
    assert historical is not None
    assert current.text == ("The currently displayed screen lists Revenue for Project A as $1.2M.")
    assert historical.text == "The checked frame listed Revenue for Project A as $1.2M."
    assert current.rendering.pattern_id == "current.entity_attribute_value"
    assert current.rendering.tense == "current"
    assert historical.rendering.tense == "historical"


@pytest.mark.parametrize("case", SUPPORTED, ids=lambda case: case["id"])
@pytest.mark.parametrize("tense", TENSES)
def test_supported_labelled_case_renders_only_from_spans_of_the_answer(
    case: dict[str, Any], tense: Tense
) -> None:
    verified = verified_case(case)

    rendered = render(verified, tense, case["language"])

    if tense == "current" and verified.claim.kind != "screen":
        assert rendered is None
        return
    assert rendered is not None
    pattern = pattern_text(tense, verified.claim.shape, case["language"])
    assert pattern is not None
    fields = {
        span.name: case["answer"][span.start : span.end] for span in rendered.rendering.fields
    }
    assert rendered.text == pattern.format(**fields)
    assert rerender(rendered.rendering, case["answer"]) == rendered.text
    assert rendered.rendering.text_digest == statement_digest(rendered.text)
    assert rendered.rendering.language == case["language"]


@pytest.mark.parametrize(
    ("answer", "value"),
    [
        ("Revenue for Project A is $1.2M. Costs fell", "$1.2M. Costs fell"),
        ("Revenue for Project A is $1.2M; stable", "$1.2M; stable"),
        ("Revenue for Project A is $1.2M, up", "$1.2M, up"),
        ("Revenue for Project A is $1.2M: confirmed", "$1.2M: confirmed"),
        ("Revenue for Project A is $1.2M\nCosts", "$1.2M\nCosts"),
        ("Revenue for Project A is $1.2M right now", "$1.2M right now"),
        ("Revenue for Project A is definitely $1.2M", "definitely $1.2M"),
        ("Revenue for Project A is not $1.2M", "not $1.2M"),
        ("Revenue for Project A is $1.2M and growing fast", "$1.2M and growing fast"),
        ("Revenue for Project A is growing fast", "growing fast"),
        ("Revenue for Project A is high", "high"),
        ("Revenue for Project A is {value}", "{value}"),
    ],
)
def test_value_field_that_carries_more_than_a_value_is_not_rendered(
    answer: str, value: str
) -> None:
    for tense in TENSES:
        assert render(revenue(answer, value=value), tense, "en") is None


@pytest.mark.parametrize(
    "value", ["$1.2M", "17 March 2027", "4%", "09:30", "1,200", '"Q3 plan"', "3 million USD"]
)
def test_value_of_a_known_type_is_rendered(value: str) -> None:
    answer = f"Revenue for Project A is {value}"

    rendered = render(revenue(answer, value=value), "current", "en")

    assert rendered is not None
    assert rendered.text.endswith(f"as {value}.")


@pytest.mark.parametrize(
    "entity",
    [
        "Project A right now",
        "Project A. Also B",
        "certainly Project A",
        "never Project A",
        " ".join(["word"] * (MAX_FIELD_WORDS + 1)),
        "x" * (MAX_FIELD_CODE_POINTS + 1),
    ],
)
def test_entity_field_with_a_marker_punctuation_or_excess_length_is_not_rendered(
    entity: str,
) -> None:
    answer = f"Revenue for {entity} is $1.2M"

    assert render(revenue(answer, entity=entity), "current", "en") is None


def test_field_at_exactly_both_limits_is_rendered_and_never_truncated() -> None:
    words = ["abcdefghi"] * MAX_FIELD_WORDS
    entity = " ".join(words)
    assert len(entity) == MAX_FIELD_CODE_POINTS - 1
    answer = f"Revenue for {entity} is $1.2M"

    rendered = render(revenue(answer, entity=entity), "current", "en")

    assert rendered is not None
    assert entity in rendered.text


def test_length_is_counted_in_code_points_after_nfc() -> None:
    composed = "é" * MAX_FIELD_CODE_POINTS
    decomposed = unicodedata.normalize("NFD", composed)
    assert len(decomposed) == 2 * MAX_FIELD_CODE_POINTS
    answer = f"Revenue for {decomposed} is $1.2M"

    assert render(revenue(answer, entity=decomposed), "current", "en") is not None
    longer = unicodedata.normalize("NFD", composed + "é")
    answer = f"Revenue for {longer} is $1.2M"
    assert render(revenue(answer, entity=longer), "current", "en") is None


@pytest.mark.parametrize("kind", ["negation_markers", "temporal_markers", "certainty_markers"])
@pytest.mark.parametrize("language", ["en", "id"])
def test_every_listed_marker_blocks_a_field_in_its_language(kind: str, language: str) -> None:
    for marker in MARKERS["languages"][language][kind]:
        if not marker[0].isalnum() or "'" in marker:
            continue
        entity = f"Project A {marker}"
        answer = f"Revenue for {entity} is $1.2M"
        assert render(revenue(answer, entity=entity), "current", language) is None, marker


def test_negative_claim_has_no_pattern() -> None:
    negative = revenue()
    negative = replace(negative, claim=negative.claim.model_copy(update={"polarity": "negative"}))

    assert render(negative, "current", "en") is None
    assert render(negative, "historical", "en") is None


def test_unsupported_claim_is_never_rendered() -> None:
    unsupported = replace(revenue(), support=Support("claim_relation_unsupported", {}))

    assert render(unsupported, "current", "en") is None


@pytest.mark.parametrize("language", ["fr", "", None])
def test_language_without_a_pattern_is_not_rendered(language: str | None) -> None:
    assert render(revenue(), "current", language) is None


def test_camera_claim_has_a_historical_pattern_and_no_current_one() -> None:
    claim = Claim(kind="participant", shape="item_presence", item="a grey shirt")
    answer = "Budi wears a grey shirt"
    verified = VerifiedClaim(
        claim, camera_evidence(), Support("ok", {"item": Span(11, 23, "a grey shirt")}), answer
    )

    assert render(verified, "current", "en") is None
    historical = render(verified, "historical", "en")
    assert historical is not None
    assert historical.text == "The checked frame showed a grey shirt."


def test_missing_field_span_is_not_rendered() -> None:
    partial = located(ANSWER, REVENUE, entity="Project A", value="$1.2M")

    assert render(partial, "current", "en") is None


def test_rendering_is_accepted_only_for_its_own_tense_version_and_text() -> None:
    rendered = render(revenue(), "current", "en")
    assert rendered is not None
    rendering = rendered.rendering

    assert rendering_refusal(rendering, "current", rendering.text_digest) is None
    assert rendering_refusal(rendering, "historical", rendering.text_digest) == NOT_RENDERED
    assert rendering_refusal(rendering, "current", statement_digest("Other.")) == NOT_RENDERED
    assert rendering_refusal(None, "current", rendering.text_digest) == NOT_RENDERED
    newer = replace(rendering, version=2)
    assert rendering_refusal(newer, "current", rendering.text_digest) == NOT_RENDERED
    assert rerender(newer, ANSWER) is None
    other = replace(rendering, language="fr")
    assert rendering_refusal(other, "current", rendering.text_digest) == NOT_RENDERED
