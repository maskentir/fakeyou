import json
import socket

import pytest

from evals._support_matcher import (
    BASES,
    LABELS,
    LANGUAGES,
    RESULTS,
    ROOT,
    Basis,
    cassette_outcomes,
    evaluate,
    sha256,
    synthetic_outcomes,
)

LABELLED = json.loads(LABELS.read_text())


@pytest.mark.parametrize("source", sorted(LABELLED["inputs"]))
def test_cassette_inputs_are_byte_identical_to_the_labelled_revision(source: str) -> None:
    assert sha256(ROOT / source) == LABELLED["inputs"][source]


def test_recorded_results_are_reproduced_from_the_cassettes_and_labels() -> None:
    assert json.loads(RESULTS.read_text()) == evaluate()


@pytest.mark.parametrize("basis", BASES)
def test_no_false_claim_is_supported_on_recorded_look_responses(basis: Basis) -> None:
    supported = [item for item in cassette_outcomes(basis) if not item.gold and item.reason == "ok"]

    assert supported == []


def test_no_unsupported_synthetic_case_is_supported_in_either_language() -> None:
    outcomes = synthetic_outcomes()

    assert {item.language for item in outcomes} == set(LANGUAGES)
    assert [item for item in outcomes if not item.gold and item.reason == "ok"] == []


def test_existing_cassettes_hold_no_indonesian_look_responses() -> None:
    results = evaluate()

    for basis in BASES:
        assert results["cassette"][basis]["id"]["claims"] == 0
        assert results["cassette"][basis]["id"]["coverage"] is None
        assert results["cassette"][basis]["en"]["claims"] == 41


def test_evaluation_makes_no_provider_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(*_args: object, **_kwargs: object) -> None:
        pytest.fail("offline evaluation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    assert evaluate()["provider_requests"] == 0
