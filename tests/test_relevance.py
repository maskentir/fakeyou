import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fakeyou.memory.relevance import content_words, relevant_edges

CASES = json.loads((Path(__file__).parent / "fixtures" / "relevance-cases.json").read_text())


def edge(fact: str) -> Any:
    return cast(Any, SimpleNamespace(fact=fact))


def test_content_words_drop_function_words_short_words_and_names() -> None:
    words = content_words("Hi Rani Twin, when is the Orchid beta launch?", ignored=["Rani Twin"])

    assert words == frozenset({"orchid", "beta", "launch"})


def test_a_fact_is_relevant_when_it_shares_two_content_words() -> None:
    edges = [
        edge("The Orchid beta launch is planned for 14 October 2026"),
        edge("Project Zebra was rescheduled to 14 November 2026"),
    ]

    kept = relevant_edges("When does the Orchid beta start?", edges)

    assert [item.fact for item in kept] == ["The Orchid beta launch is planned for 14 October 2026"]


def test_the_owner_s_name_alone_does_not_make_a_fact_relevant() -> None:
    edges = [edge("Rani owns the launch checklist")]

    assert relevant_edges("Rani, how was your weekend?", edges, ignored=["Rani Twin"]) == []


def test_product_terms_match_across_languages() -> None:
    case = CASES["cross_language"]
    edges = [edge(case["fact"])]

    assert relevant_edges(case["question"], edges, ignored=["Rani Twin"]) == edges


def test_nothing_is_relevant_to_a_question_without_content_words() -> None:
    case = CASES["function_words_only"]

    assert relevant_edges("Can you hear me?", [edge("Rani can hear the room")]) == []
    assert relevant_edges(case["question"], [edge(case["fact"])], ignored=["Rani Twin"]) == []


def test_indonesian_word_forms_match_their_root() -> None:
    for name in ("inflected_forms", "affixed_forms"):
        case = CASES[name]
        edges = [edge(case["fact"])]

        assert relevant_edges(case["question"], edges) == edges, name


def test_words_are_never_cut_to_a_different_root() -> None:
    case = CASES["lookalike_roots"]

    assert relevant_edges(case["question"], [edge(case["fact"])]) == []


def test_english_words_keep_their_form() -> None:
    assert content_words("The meeting venue for the launch") == frozenset(
        {"meeting", "venue", "launch"}
    )
