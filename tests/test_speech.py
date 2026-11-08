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

SPOKEN_NUMBERS = json.loads(
    (Path(__file__).parent / "fixtures" / "spoken-numbers.json").read_text(encoding="utf-8")
)
OBJECTIVE = "Find out whether the vendor can deliver the integration before the end of the quarter"
BOOK = FactBook(
    entries={
        "brief:launch": BookEntry("brief:launch", "Beta launches on 14 October.", "brief"),
        "brief:q1": BookEntry(
            "brief:q1", "Can the vendor ship the API by Q4?", "brief", question=True
        ),
        "brief:price": BookEntry(
            "brief:price", "The floor price is 120 million.", "brief", disclosure="owner_only"
        ),
        "memory:m1": BookEntry("memory:m1", "Ayu owns the data migration.", "memory"),
    },
    heard="Ayu: we planned 3 sprints for this.",
    internal=(OBJECTIVE,),
)


def claim(*refs: str, act: str = "fact") -> ClaimHeader:
    return ClaimHeader.model_validate(
        {
            "act": act,
            "basis": "brief" if refs else "none",
            "refs": refs,
            "certainty": "stated",
            "speaks_as": "reporting" if refs else "twin",
        }
    )


def test_a_cited_number_is_speakable() -> None:
    assert check_claim(claim("brief:launch"), BOOK, proactive=False) == "pass"
    assert check_sentence("Beta launches on 14 October.", ("brief:launch",), BOOK) == "pass"


def test_a_number_said_in_the_room_may_be_repeated() -> None:
    assert check_sentence("So that is 3 sprints.", (), BOOK) == "pass"


def test_an_invented_number_is_rejected_before_it_is_spoken() -> None:
    assert check_sentence("Beta launches on 21 October.", ("brief:launch",), BOOK) == (
        "invented_number"
    )
    assert check_sentence("It costs 99 dollars.", (), BOOK) == "invented_number"


def test_an_owner_only_fact_cannot_be_cited_aloud() -> None:
    assert check_claim(claim("brief:price"), BOOK, proactive=False) == "not_speakable"


def test_a_citation_must_exist() -> None:
    assert check_claim(claim("memory:ghost"), BOOK, proactive=False) == "unknown_citation"


def test_committing_for_the_owner_is_rejected() -> None:
    assert check_plan([claim()], commits_owner=True, proactive=False) == "owner_commitment"


def test_internal_objectives_are_never_read_out() -> None:
    leak = "I need to find out whether the vendor can deliver the integration before the end."

    assert check_sentence(leak, (), BOOK) == "instruction_disclosure"
    assert check_sentence("Can you deliver the integration soon?", (), BOOK) == "pass"


def test_a_contribution_must_cite_and_a_question_must_come_from_the_brief() -> None:
    assert check_plan([claim()], commits_owner=False, proactive=True) == "uncited_contribution"
    assert check_plan([claim(), claim("memory:m1")], commits_owner=False, proactive=True) == (
        "pass"
    )
    assert check_claim(claim("brief:launch", act="question"), BOOK, proactive=True) == (
        "question_not_in_brief"
    )
    assert check_claim(claim("brief:q1", act="question"), BOOK, proactive=True) == "pass"


def test_a_direct_answer_may_ask_without_a_brief_question() -> None:
    assert check_plan([claim()], commits_owner=False, proactive=False) == "pass"
    assert check_claim(claim(act="question"), BOOK, proactive=False) == "pass"


def test_numbers_are_compared_by_their_written_form() -> None:
    assert numbers_in("At 10:30, 2.5 million, and 14/10.") == frozenset({"10:30", "2.5", "14/10"})


def test_a_number_spoken_as_words_is_not_treated_as_invented() -> None:
    book = FactBook(heard="the number is fifty", entries={})

    assert check_sentence("The number is 50.", (), book) == "pass"
    assert check_sentence("The number is 42.", (), book) == "invented_number"


def test_a_compound_number_in_words_is_read_as_one_number() -> None:
    book = FactBook(heard="the line forty-two", entries={})

    assert check_sentence("Line 42.", (), book) == "pass"


def test_a_number_inside_a_longer_spoken_number_is_not_grounded() -> None:
    book = FactBook(heard="we have fifty-five days", entries={})

    assert check_sentence("We have 55 days.", (), book) == "pass"
    assert check_sentence("We have 5 days.", (), book) == "invented_number"


def test_one_used_as_a_pronoun_does_not_ground_a_digit() -> None:
    book = FactBook(heard="no one approved it", entries={})

    assert check_sentence("Budget 1 million.", (), book) == "invented_number"


@pytest.mark.parametrize("case", SPOKEN_NUMBERS["grounding"], ids=lambda case: case["heard"])
def test_numbers_heard_in_indonesian_words_ground_only_their_own_digits(
    case: dict[str, Any],
) -> None:
    book = FactBook(heard=case["heard"], entries={})

    assert all(check_sentence(line, (), book) == "pass" for line in case["pass"])
    assert all(check_sentence(line, (), book) == "invented_number" for line in case["invented"])


def test_spoken_numbers_split_at_punctuation() -> None:
    phrases = SPOKEN_NUMBERS["phrases"]

    assert spoken_numbers(phrases["text"]) == {tuple(run) for run in phrases["runs"]}
