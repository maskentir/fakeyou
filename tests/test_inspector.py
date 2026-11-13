import pytest
from pydantic import ValidationError

from fakeyou.perception.inspector import VisualInspection


@pytest.mark.parametrize("verdict", ["not_visible", "unclear"])
def test_an_abstention_has_no_answer(verdict: str) -> None:
    result = VisualInspection.model_validate({"verdict": verdict})
    assert result.answer is None


@pytest.mark.parametrize(
    "payload",
    [
        '{"verdict":"supported"}',
        '{"verdict":"supported","answer":"  "}',
        '{"verdict":"not_visible","answer":"blue"}',
        '{"verdict":"unclear","answer":"blue"}',
        '{"verdict":"verified","answer":"blue"}',
        '{"verdict":"supported","answer":"blue","confidence":1}',
        '{"verdict":"supported","answer":"blue","observed_facts":[""]}',
        '{"verdict":"supported","answer":42}',
        "not json",
    ],
)
def test_invalid_inspections_fail_validation(payload: str) -> None:
    with pytest.raises(ValidationError):
        VisualInspection.model_validate_json(payload)


def test_supported_is_a_model_verdict_not_verification() -> None:
    result = VisualInspection.model_validate_json(
        '{"verdict":"supported","answer":"red","observed_facts":["red shirt"]}'
    )
    assert result.verdict == "supported"
    assert result.answer == "red"
    assert not hasattr(result, "verified")
