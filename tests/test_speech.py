import json
from pathlib import Path
from typing import Any

import pytest

from fakeyou.session.speech import (
    BookEntry,
    ClaimHeader,
    FactBook,
    check_claim,
    check_plan,
    check_sentence,
    numbers_in,
    spoken_numbers,
)

FIXTURES = Path(__file__).parent / "fixtures"
SPOKEN_NUMBERS = json.loads((FIXTURES / "spoken-numbers.json").read_text(encoding="utf-8"))
CLAIM_CASES = json.loads((FIXTURES / "claim-cases.json").read_text(encoding="utf-8"))
OBJECTIVE = "Find out whether the vendor can deliver the integration before the end of the quarter"
BOOK = FactBook(
    entries={
        "brief:launch": BookEntry("brief:launch", "Beta launches on 14 October.", "brief"),
        "brief:q1": BookEntry(
            "brief:q1", "Can the vendor ship the API by Q4?", "brief", kind="question"
        ),
        "brief:o1": BookEntry(
            "brief:o1", OBJECTIVE, "brief", disclosure="internal_instruction", kind="objective"
        ),
        "brief:price": BookEntry(
            "brief:price", "The floor price is 120 million.", "brief", disclosure="owner_only"
        ),
        "memory:m1": BookEntry("memory:m1", "Ayu owns the data migration.", "memory"),
        "memory:owner": BookEntry(
            "memory:owner", "Rani said she prefers weekly demos.", "memory", owner_spoken=True
        ),
        "om-ship": BookEntry(
            "om-ship", "Ship after the load test passes.", "owner_model", kind="position"
        ),
        "om-vendor": BookEntry(
            "om-vendor",
            "Leaning towards the second vendor.",
            "owner_model",
            kind="preference",
            confidence="leaning",
        ),
        "om-city": BookEntry("om-city", "Rani lives in Bandung.", "owner_model"),
    },
    heard="Ayu: we planned 3 sprints for this.",
    internal=(OBJECTIVE,),
)


def claim(
    *refs: str,
    act: str = "fact",
    basis: str | None = None,
    certainty: str = "stated",
    speaks_as: str | None = None,
) -> ClaimHeader:
    return ClaimHeader.model_validate(
        {
            "act": act,
            "basis": basis or ("brief" if refs else "none"),
            "refs": refs,
            "certainty": certainty,
            "speaks_as": speaks_as or ("reporting" if refs else "twin"),
        }
    )


def owner(*refs: str, act: str = "opinion", certainty: str = "stated") -> ClaimHeader:
    return claim(*refs, act=act, basis="owner_model", certainty=certainty, speaks_as="owner_proxy")


def test_a_cited_number_is_speakable() -> None:
    launch = claim("brief:launch")

    assert check_claim(launch, BOOK) == "pass"
    assert check_sentence("Beta launches on 14 October.", launch, BOOK) == "pass"


def test_a_number_said_in_the_room_may_be_repeated() -> None:
    assert check_sentence("So that is 3 sprints.", None, BOOK) == "pass"


def test_an_invented_number_is_rejected_before_it_is_spoken() -> None:
    assert check_sentence("Beta launches on 21 October.", claim("brief:launch"), BOOK) == (
        "invented_number"
    )
    assert check_sentence("It costs 99 dollars.", None, BOOK) == "invented_number"


def test_an_owner_only_fact_cannot_be_cited_aloud() -> None:
    assert check_claim(claim("brief:price"), BOOK) == "not_speakable"


def test_a_citation_must_exist() -> None:
    assert check_claim(claim("memory:ghost", basis="memory"), BOOK) == "unknown_citation"


def test_committing_for_the_owner_is_rejected() -> None:
    assert check_plan([claim()], commits_owner=True, proactive=False) == "owner_commitment"


def test_internal_objectives_are_never_read_out() -> None:
    leak = "I need to find out whether the vendor can deliver the integration before the end."
    asking = claim("brief:o1", act="question", speaks_as="owner_proxy")

    assert check_sentence(leak, None, BOOK) == "instruction_disclosure"
    assert check_sentence(leak, asking, BOOK) == "instruction_disclosure"
    assert check_sentence("Can you deliver the integration soon?", None, BOOK) == "pass"


@pytest.mark.parametrize("case", CLAIM_CASES["unbased_personal"], ids=lambda case: case["language"])
def test_a_personal_fact_without_a_basis_is_refused(case: dict[str, str]) -> None:
    about_myself = claim(act="meta")

    assert check_claim(claim(), BOOK) == "unbased_claim"
    assert check_claim(about_myself, BOOK) == "pass"
    assert check_sentence(case["say"], about_myself, BOOK) == "pass"


@pytest.mark.parametrize("case", CLAIM_CASES["meta"], ids=lambda case: case["language"])
def test_meta_speaks_only_about_the_twin_without_numbers(case: dict[str, str]) -> None:
    about_myself = claim(act="meta")

    assert check_sentence(case["pass"], about_myself, BOOK) == "pass"
    assert check_sentence(case["counted"], about_myself, BOOK) == "meta_with_content"
    assert check_claim(claim("memory:m1", act="meta", basis="memory"), BOOK) == (
        "meta_with_content"
    )


def test_refs_must_belong_to_the_namespace_the_basis_names() -> None:
    assert check_claim(claim("memory:m1"), BOOK) == "basis_mismatch"
    assert check_claim(claim("brief:launch", basis="meeting_live"), BOOK) == "basis_mismatch"
    assert check_claim(claim(basis="owner_model", speaks_as="owner_proxy"), BOOK) == (
        "basis_mismatch"
    )
    assert check_claim(claim("memory:m1", basis="memory"), BOOK) == "pass"


def test_an_opinion_needs_an_owner_position() -> None:
    assert check_claim(owner("om-city"), BOOK) == "opinion_without_position"
    assert check_claim(owner("om-ship"), BOOK) == "pass"
    assert check_claim(owner("om-ship", act="proposal"), BOOK) == "pass"
    assert check_claim(claim(act="opinion", basis="inference", certainty="guess"), BOOK) == (
        "opinion_without_position"
    )


def test_a_question_rests_on_the_brief_or_an_owner_position() -> None:
    assert check_claim(claim("brief:o1", act="question"), BOOK) == "pass"
    assert check_claim(claim("brief:q1", act="question"), BOOK) == "pass"
    assert check_claim(owner("om-ship", act="question"), BOOK) == "pass"
    assert check_claim(claim("brief:launch", act="question"), BOOK) == "question_without_basis"


def test_an_objective_is_cited_only_by_a_question() -> None:
    assert check_claim(claim("brief:o1"), BOOK) == "not_speakable"


def test_a_leaning_item_and_an_inference_are_said_as_guesses() -> None:
    assert check_claim(owner("om-vendor"), BOOK) == "guess_as_fact"
    assert check_claim(owner("om-vendor", certainty="guess"), BOOK) == "pass"
    assert check_claim(claim(basis="inference"), BOOK) == "guess_as_fact"
    assert check_claim(claim(basis="inference", certainty="guess"), BOOK) == "pass"


def test_the_voice_follows_the_basis() -> None:
    owner_memory = claim("memory:owner", basis="memory", speaks_as="owner_proxy")
    participant_memory = claim("memory:m1", basis="memory", speaks_as="owner_proxy")
    live = claim(basis="meeting_live", speaks_as="owner_proxy")
    guess = claim(basis="inference", certainty="guess", speaks_as="reporting")

    assert check_claim(owner_memory, BOOK) == "pass"
    assert check_claim(participant_memory, BOOK) == "speaks_as_mismatch"
    assert check_claim(live, BOOK) == "speaks_as_mismatch"
    assert check_claim(guess, BOOK) == "speaks_as_mismatch"


def test_a_contribution_needs_a_sourced_claim() -> None:
    meta = claim(act="meta")
    guess = claim(basis="inference", certainty="guess")
    recalled = claim("memory:m1", basis="memory")

    assert check_plan([meta, guess], commits_owner=False, proactive=True) == "unbased_claim"
    assert check_plan([meta, recalled], commits_owner=False, proactive=True) == "pass"
    assert check_plan([meta, guess], commits_owner=False, proactive=False) == "pass"


def test_numbers_are_compared_by_their_written_form() -> None:
    assert numbers_in("At 10:30, 2.5 million, and 14/10.") == frozenset({"10:30", "2.5", "14/10"})


def test_a_number_spoken_as_words_is_not_treated_as_invented() -> None:
    book = FactBook(heard="the number is fifty", entries={})

    assert check_sentence("The number is 50.", None, book) == "pass"
    assert check_sentence("The number is 42.", None, book) == "invented_number"


def test_a_compound_number_in_words_is_read_as_one_number() -> None:
    book = FactBook(heard="the line forty-two", entries={})

    assert check_sentence("Line 42.", None, book) == "pass"


def test_a_number_inside_a_longer_spoken_number_is_not_grounded() -> None:
    book = FactBook(heard="we have fifty-five days", entries={})

    assert check_sentence("We have 55 days.", None, book) == "pass"
    assert check_sentence("We have 5 days.", None, book) == "invented_number"


def test_one_used_as_a_pronoun_does_not_ground_a_digit() -> None:
    book = FactBook(heard="no one approved it", entries={})

    assert check_sentence("Budget 1 million.", None, book) == "invented_number"


@pytest.mark.parametrize("case", SPOKEN_NUMBERS["grounding"], ids=lambda case: case["heard"])
def test_numbers_heard_in_indonesian_words_ground_only_their_own_digits(
    case: dict[str, Any],
) -> None:
    book = FactBook(heard=case["heard"], entries={})

    assert all(check_sentence(line, None, book) == "pass" for line in case["pass"])
    assert all(check_sentence(line, None, book) == "invented_number" for line in case["invented"])


def test_spoken_numbers_split_at_punctuation() -> None:
    phrases = SPOKEN_NUMBERS["phrases"]

    assert spoken_numbers(phrases["text"]) == {tuple(run) for run in phrases["runs"]}
