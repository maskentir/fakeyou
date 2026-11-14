import json
from pathlib import Path

from evals._provider_doubles import (
    Cassette,
    RequestKey,
    _adapt_look_to_inspection,
    _is_look_request,
    _token_vector,
)
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.interpreter import INSPECTION_PROMPT

JUDGE = "You decide whether the twin should take the floor."
EARLIER = "Ayu: " + "the slides look fine and the checklist is on track. " * 6
QUESTION = "Harun: Twin, please raise your hand"


def _gemini_entry(answer: str) -> dict[str, object]:
    body = json.dumps({"candidates": [{"content": {"parts": [{"text": answer}], "role": "model"}}]})
    return {
        "route": "/v1beta/models/gemini-3.8-flash:generateContent",
        "system": "",
        "focus": "focus",
        "status": 200,
        "media_type": "application/json",
        "chunks": [[0.0, body]],
    }


def test_a_recorded_free_text_look_replays_as_a_structured_inspection() -> None:
    adapted = _adapt_look_to_inspection(_gemini_entry("Harun's shirt is red."))

    body = json.loads("".join(chunk[1] for chunk in adapted["chunks"]))
    text = body["candidates"][0]["content"]["parts"][0]["text"]

    assert json.loads(text) == {"verdict": "supported", "answer": "Harun's shirt is red."}
    inspection = VisualInspection.model_validate_json(text)
    assert inspection.verdict == "supported"
    assert inspection.answer == "Harun's shirt is red."


def test_a_look_request_is_recognised_by_the_inspection_prompt() -> None:
    look = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"inlineData": {"mimeType": "image/jpeg", "data": "<image>"}},
                        {"text": INSPECTION_PROMPT.format(source="a camera", question="color?")},
                    ],
                }
            ]
        }
    ).encode()
    reading = json.dumps(
        {"contents": [{"role": "user", "parts": [{"text": "Fill every field from the screen"}]}]}
    ).encode()

    assert _is_look_request(look) is True
    assert _is_look_request(reading) is False


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
