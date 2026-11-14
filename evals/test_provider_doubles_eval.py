import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import _provider_doubles as doubles_module
from evals._provider_doubles import (
    Cassette,
    ProviderDoubles,
    RequestKey,
    _is_look_request,
    _token_vector,
)
from evals._visual_cassette import ExperimentBudget
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.interpreter import INSPECTION_PROMPT, INSPECTION_PROMPT_MARKER

JUDGE = "You decide whether the twin should take the floor."
EARLIER = "Ayu: " + "the slides look fine and the checklist is on track. " * 6
QUESTION = "Harun: Twin, please raise your hand"
CASSETTES = Path(__file__).parent / "fixtures" / "simulator" / "cassettes"


@pytest.mark.parametrize("closed_ledger", [False, True])
async def test_full_recording_does_not_change_the_visual_experiment_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, closed_ledger: bool
) -> None:
    ledger = tmp_path / "experiment-attempts.jsonl"
    budget = ExperimentBudget(max_requests=1, ledger=ledger)
    if closed_ledger:
        budget.reserve({"source": "test"})
        assert budget.exhausted
    before = ledger.read_bytes() if ledger.exists() else None
    monkeypatch.setattr(doubles_module, "ExperimentBudget", lambda: budget)
    cassette = Cassette.load(tmp_path / "full-recording.jsonl")
    doubles = ProviderDoubles("record", cassette)
    payload = {"contents": [{"parts": [{"text": INSPECTION_PROMPT_MARKER}]}]}
    recorded = {"usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=recorded)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as upstream:
        monkeypatch.setattr(doubles, "_client", upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=doubles.app()), base_url="http://double"
        ) as client:
            response = await client.post("/v1beta/models/test:generateContent", json=payload)

    assert response.status_code == 200
    assert response.json() == recorded
    [entry] = Cassette.load(cassette.path).entries
    assert json.loads("".join(chunk[1] for chunk in entry["chunks"])) == recorded
    assert (ledger.read_bytes() if ledger.exists() else None) == before


def _gemini_entry(answer: str) -> dict[str, Any]:
    body = json.dumps({"candidates": [{"content": {"parts": [{"text": answer}], "role": "model"}}]})
    return {
        "route": "/v1beta/models/gemini-3.8-flash:generateContent",
        "system": "",
        "focus": "focus",
        "status": 200,
        "media_type": "application/json",
        "chunks": [[0.0, body]],
    }


def _recorded_look_entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(CASSETTES.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entry = json.loads(line)
            if INSPECTION_PROMPT_MARKER in str(entry.get("focus", "")):
                entries.append(entry)
    return entries


def test_every_recorded_simulator_look_is_a_structured_inspection() -> None:
    entries = _recorded_look_entries()

    assert entries, "the simulator cassettes must hold the recorded look requests"

    for entry in entries:
        body = "".join(chunk[1] for chunk in entry.get("chunks", []) if len(chunk) > 1)
        text = json.loads(body)["candidates"][0]["content"]["parts"][0]["text"]
        inspection = VisualInspection.model_validate_json(text)
        assert inspection.verdict in {"supported", "not_visible", "unclear"}


def test_a_recorded_free_text_look_is_rejected_by_the_inspection_contract() -> None:
    free_text = _gemini_entry("Harun's shirt is red.")
    body = "".join(chunk[1] for chunk in free_text["chunks"])
    text = json.loads(body)["candidates"][0]["content"]["parts"][0]["text"]

    with pytest.raises(ValueError, match="expected value"):
        VisualInspection.model_validate_json(text)


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
