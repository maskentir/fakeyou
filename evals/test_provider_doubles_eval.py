from pathlib import Path

from evals._provider_doubles import Cassette, RequestKey, _token_vector

JUDGE = "You decide whether the twin should take the floor."
EARLIER = "Ayu: " + "the slides look fine and the checklist is on track. " * 6
QUESTION = "Harun: Twin, please raise your hand"


def _entry(focus: str, verdict: str) -> dict[str, str]:
    return {"route": "judge", "system": JUDGE, "focus": focus, "verdict": verdict}


def test_the_embedding_double_is_deterministic_and_orders_shared_words_first() -> None:
    slide = _token_vector("Beta launch is scheduled for 21 October 2026", 1024)
    brief = _token_vector("The Orchid beta launch is planned for 14 October 2026", 1024)
    other = _token_vector("Quarterly revenue grew in the northern region", 1024)

    assert slide == _token_vector("Beta launch is scheduled for 21 October 2026", 1024)
    related = sum(left * right for left, right in zip(slide, brief, strict=True))
    unrelated = sum(left * right for left, right in zip(slide, other, strict=True))
    assert related > 0.15 > unrelated


def test_repeated_requests_take_recorded_answers_in_order(tmp_path: Path) -> None:
    cassette = Cassette(
        tmp_path / "judge.jsonl",
        [
            _entry(EARLIER + QUESTION, "silent"),
            _entry("Budi: morning all. " + EARLIER + QUESTION + ".", "reply"),
        ],
    )
    asked = RequestKey(JUDGE, "Budi: morning all. " + EARLIER + QUESTION)

    first = cassette.match("judge", asked)
    second = cassette.match("judge", asked)

    assert first is not None
    assert second is not None
    assert (first["verdict"], second["verdict"]) == ("silent", "reply")
    assert cassette.reused == 0


def test_a_different_question_never_takes_an_unused_answer(tmp_path: Path) -> None:
    cassette = Cassette(
        tmp_path / "judge.jsonl",
        [
            _entry(EARLIER + QUESTION, "silent"),
            _entry(EARLIER + "Harun: Twin, what is the beta date?", "reply"),
        ],
    )
    asked = RequestKey(JUDGE, EARLIER + QUESTION)

    cassette.match("judge", asked)
    again = cassette.match("judge", asked)

    assert again is not None
    assert again["verdict"] == "silent"
    assert cassette.reused == 1
