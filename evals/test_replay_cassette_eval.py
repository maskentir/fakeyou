import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

from evals._replay_cassette import (
    VOLATILE_RULES,
    CassetteError,
    HeardTranscripts,
    ModelExchange,
    ReplayLedger,
    ReplayMiss,
    SpeechMessage,
    canonical_request,
    model_exchange,
    request_digest,
)

ROUTE = "/v1beta/models/gemini-3.1-flash-lite:generateContent"
JUDGE = "You read a live meeting for a digital twin and decide who the latest line is for."
REPLY = "You are a digital twin for a meeting participant."
PIXELS = b"\xff\xd8\xff\xe0red-shirt-frame"


def _body(
    question: str,
    *,
    system: str = JUDGE,
    history: tuple[str, ...] = (),
    image: bytes | None = None,
    temperature: float = 0.0,
) -> bytes:
    parts: list[dict[str, Any]] = [{"text": question}]
    if image is not None:
        parts.insert(
            0, {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode()}}
        )
    contents = [{"role": "user", "parts": [{"text": line}]} for line in history]
    contents.append({"role": "user", "parts": parts})
    return json.dumps(
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
    ).encode()


def _answer(text: str) -> tuple[tuple[float, str], ...]:
    return ((0.0, json.dumps({"candidates": [{"content": {"parts": [{"text": text}]}}]})),)


def _exchange(body: bytes, answer: str, route: str = ROUTE) -> ModelExchange:
    return model_exchange(canonical_request(route, body), 200, "application/json", _answer(answer))


def _write(path: Path, lines: list[ModelExchange | SpeechMessage]) -> Path:
    path.write_text("".join(line.model_dump_json() + "\n" for line in lines), encoding="utf-8")
    return path


def _ledger(tmp_path: Path, *lines: ModelExchange | SpeechMessage) -> ReplayLedger:
    return ReplayLedger.load(_write(tmp_path / "scenario.jsonl", list(lines)))


def _text(answer: ModelExchange | ReplayMiss) -> str:
    assert isinstance(answer, ModelExchange), answer
    return str(json.loads(answer.chunks[0][1])["candidates"][0]["content"]["parts"][0]["text"])


def _speech(clip: str, take: int, text: str) -> SpeechMessage:
    return SpeechMessage(
        kind="speech",
        clip=clip,
        take=take,
        offset=0.5,
        message={"type": "transcript", "transcript": text},
    )


def test_an_identical_request_replays_its_own_recorded_answer(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _exchange(_body("Twin, raise your hand"), "raise_hand"))

    assert _text(ledger.respond(ROUTE, _body("Twin, raise your hand"))) == "raise_hand"
    assert ledger.summary(HeardTranscripts())["closed"] is True


@pytest.mark.parametrize(
    ("asked", "reason"),
    [
        (_body("Twin, what is the beta date?"), "a different question"),
        (_body("Twin, raise your hand", system=JUDGE + " Be brief."), "a changed system prompt"),
        (_body("Twin, raise your hand", history=("Ayu: hello",)), "different earlier turns"),
        (_body("Twin, raise your hand", temperature=0.7), "a changed inference setting"),
        (_body("Twin, raise your hand", image=PIXELS), "an added image"),
        (_body("Twin, raise your", system=JUDGE), "a shorter interim of the same line"),
    ],
)
def test_a_request_that_differs_in_anything_behavioural_is_refused(
    tmp_path: Path, asked: bytes, reason: str
) -> None:
    ledger = _ledger(tmp_path, _exchange(_body("Twin, raise your hand"), "raise_hand"))

    answer = ledger.respond(ROUTE, asked)

    assert isinstance(answer, ReplayMiss), reason
    assert answer.kind == "unrecorded_request"
    assert ledger.summary(HeardTranscripts())["closed"] is False


def test_a_question_never_receives_another_questions_answer(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        _exchange(_body("Twin, raise your hand"), "raise_hand"),
        _exchange(_body("Twin, send a heart"), "send_heart"),
    )

    assert _text(ledger.respond(ROUTE, _body("Twin, send a heart"))) == "send_heart"
    assert _text(ledger.respond(ROUTE, _body("Twin, raise your hand"))) == "raise_hand"
    assert isinstance(ledger.respond(ROUTE, _body("Twin, send a thumbs up")), ReplayMiss)


def test_another_model_route_does_not_share_a_recording(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _exchange(_body("Twin, raise your hand"), "raise_hand"))

    answer = ledger.respond(
        "/v1beta/models/gemini-3.8-flash:generateContent", _body("Twin, raise your hand")
    )

    assert isinstance(answer, ReplayMiss)


def test_a_changed_frame_cannot_reuse_a_recorded_visual_answer(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path, _exchange(_body("What colour is Harun's shirt?", image=PIXELS), "red")
    )

    stale = ledger.respond(ROUTE, _body("What colour is Harun's shirt?", image=PIXELS + b"blue"))
    same = ledger.respond(ROUTE, _body("What colour is Harun's shirt?", image=PIXELS))

    assert isinstance(stale, ReplayMiss)
    assert _text(same) == "red"


def test_the_canonical_request_keeps_image_identity_but_not_image_bytes() -> None:
    request = canonical_request(ROUTE, _body("look", image=PIXELS))

    serialized = json.dumps(request)

    assert base64.b64encode(PIXELS).decode() not in serialized
    assert '"sha256"' in serialized


def test_identical_repeats_are_served_in_recorded_order_and_an_extra_repeat_is_refused(
    tmp_path: Path,
) -> None:
    body = _body("Twin, are you there?")
    ledger = _ledger(tmp_path, _exchange(body, "first"), _exchange(body, "second"))

    answers = [ledger.respond(ROUTE, body) for _ in range(3)]

    assert [_text(answer) for answer in answers[:2]] == ["first", "second"]
    assert isinstance(answers[2], ReplayMiss)
    assert answers[2].kind == "repeated_request"


async def test_concurrent_requests_each_receive_their_own_recording(tmp_path: Path) -> None:
    questions = [f"Twin, question number {index}" for index in range(8)]
    ledger = _ledger(tmp_path, *(_exchange(_body(q), q.upper()) for q in questions))

    async def ask(question: str) -> str:
        await asyncio.sleep(0)
        return _text(ledger.respond(ROUTE, _body(question)))

    answers = await asyncio.gather(*(ask(question) for question in reversed(questions)))

    assert answers == [question.upper() for question in reversed(questions)]


def test_unused_recordings_keep_the_replay_open(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        _exchange(_body("Twin, raise your hand"), "raise_hand"),
        _exchange(_body("Twin, lower your hand"), "lower_hand"),
    )

    ledger.respond(ROUTE, _body("Twin, raise your hand"))
    summary: Any = ledger.summary(HeardTranscripts())

    assert summary["closed"] is False
    assert [item["request_sha256"] for item in summary["unused"]] == [
        request_digest(canonical_request(ROUTE, _body("Twin, lower your hand")))
    ]


def test_a_miss_names_the_nearest_recording_only_as_a_diagnostic(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, _exchange(_body("Twin, raise your hand"), "raise_hand"))

    miss = ledger.respond(ROUTE, _body("Twin, lower your hand"))

    assert isinstance(miss, ReplayMiss)
    assert miss.nearest is not None
    assert "lower" in miss.nearest
    assert "raise" in miss.nearest
    assert ledger.served.total() == 0


def test_each_spoken_take_replays_its_own_recording_even_on_a_reconnect(
    tmp_path: Path,
) -> None:
    ledger = _ledger(
        tmp_path,
        _speech("raise-hand.wav", 1, "Twin, raise your hand"),
        _speech("raise-hand.wav", 2, "Twin, raise your hand please"),
    )

    first = ledger.speech("raise-hand.wav", 1)
    reconnected = ledger.speech("raise-hand.wav", 1)
    second = ledger.speech("raise-hand.wav", 2)
    third = ledger.speech("raise-hand.wav", 3)
    unknown = ledger.speech("leave.wav", 1)

    assert isinstance(first, tuple)
    assert isinstance(second, tuple)
    assert first == reconnected
    assert first[0].message["transcript"] == "Twin, raise your hand"
    assert second[0].message["transcript"] == "Twin, raise your hand please"
    assert isinstance(third, ReplayMiss)
    assert third.kind == "unrecorded_clip"
    assert isinstance(unknown, ReplayMiss)
    assert unknown.kind == "unrecorded_clip"


def test_a_recorded_take_that_is_never_spoken_keeps_the_replay_open(tmp_path: Path) -> None:
    ledger = _ledger(
        tmp_path,
        _speech("raise-hand.wav", 1, "Twin, raise your hand"),
        _speech("leave.wav", 1, "you can leave"),
    )

    ledger.speech("raise-hand.wav", 1)
    summary: Any = ledger.summary(HeardTranscripts())

    assert summary["unused"] == [{"route": "stt", "clip": "leave.wav", "take": 1}]
    assert summary["closed"] is False


@pytest.mark.parametrize(
    ("content", "complaint"),
    [
        ('{"kind": "model", "route": "x"}\n', "does not follow the cassette contract"),
        (
            '{"route": "stt", "clip": "a.wav", "take": "9f48", "offset": 0.1, "message": {}}\n',
            "does not follow",
        ),
        ("not json\n", "does not follow"),
    ],
)
def test_a_malformed_or_outdated_cassette_line_is_rejected(
    tmp_path: Path, content: str, complaint: str
) -> None:
    path = tmp_path / "scenario.jsonl"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(CassetteError, match=complaint):
        ReplayLedger.load(path)


def test_a_recording_whose_digest_does_not_match_its_request_is_rejected(tmp_path: Path) -> None:
    exchange = _exchange(_body("Twin, raise your hand"), "raise_hand")
    tampered = exchange.model_copy(
        update={"request": {**exchange.request, "route": "/v1beta/models/other:generateContent"}}
    )

    with pytest.raises(CassetteError, match="request digest that does not match"):
        _ledger(tmp_path, tampered)


def test_missing_cassettes_and_gapped_takes_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(CassetteError, match="record it"):
        ReplayLedger.load(tmp_path / "absent.jsonl")
    with pytest.raises(CassetteError, match="not numbered"):
        _ledger(tmp_path, _speech("leave.wav", 2, "leave"))


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        (b"raise your hand", "not JSON"),
        (
            b'{"contents": [{"parts": [{"inlineData": {"data": "not*base64"}}]}]}',
            "not valid base64",
        ),
    ],
)
def test_a_request_that_cannot_be_canonicalised_is_a_fatal_recorded_miss(
    tmp_path: Path, body: bytes, complaint: str
) -> None:
    ledger = _ledger(tmp_path, _exchange(_body("Twin, raise your hand"), "raise_hand"))

    miss = ledger.respond(ROUTE, body)
    summary: Any = ledger.summary(HeardTranscripts())

    assert isinstance(miss, ReplayMiss)
    assert miss.kind == "malformed_request"
    assert complaint in miss.detail
    assert summary["closed"] is False


def test_url_safe_and_standard_base64_of_the_same_image_share_one_identity() -> None:
    pixels = bytes(range(256)) * 3
    standard = base64.b64encode(pixels).decode()
    url_safe = base64.urlsafe_b64encode(pixels).decode()

    def asked(data: str) -> dict[str, Any]:
        part = {"inlineData": {"mimeType": "image/jpeg", "data": data}}
        return canonical_request(ROUTE, json.dumps({"contents": [{"parts": [part]}]}).encode())

    assert standard != url_safe
    assert request_digest(asked(standard)) == request_digest(asked(url_safe))


def test_volatile_rules_are_declared_and_named() -> None:
    assert all(rule.name and rule.placeholder for rule in VOLATILE_RULES)
    assert len({rule.name for rule in VOLATILE_RULES}) == len(VOLATILE_RULES)


def _window(latest: str) -> bytes:
    return _body(json.dumps({"earlier": [], "latest": {"speaker": "Speaker A", "text": latest}}))


def _heard(interim: tuple[str, ...], final: tuple[str, ...]) -> HeardTranscripts:
    heard = HeardTranscripts()
    for text in interim:
        heard.note({"type": "transcript", "transcript": f" {text}", "final": False})
    for text in final:
        heard.note({"type": "transcript", "transcript": f" {text}", "final": True})
    return heard


def test_an_unrecorded_judgement_of_an_abandoned_interim_is_reported_but_not_fatal(
    tmp_path: Path,
) -> None:
    final = "Rani Twin, can you walk me through it?"
    ledger = _ledger(tmp_path, _exchange(_window(final), "reply"))
    heard = _heard(("Rani Twin, can", "Rani Twin, can you"), (final,))

    miss = ledger.respond(ROUTE, _window("Rani Twin, can you"))
    served = ledger.respond(ROUTE, _window(final))
    summary: Any = ledger.summary(heard)

    assert isinstance(miss, ReplayMiss)
    assert _text(served) == "reply"
    assert summary["closed"] is True
    assert [item["judge_latest"] for item in summary["speculative_misses"]] == [
        "Rani Twin, can you"
    ]


def test_a_judgement_of_text_that_became_final_is_never_treated_as_speculative(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path, _exchange(_window("Rani Twin, raise your hand"), "raise_hand"))
    spoken = "Rani Twin, raise your hand please"

    ledger.respond(ROUTE, _window(spoken))
    summary: Any = ledger.summary(_heard((spoken,), (spoken,)))

    assert summary["closed"] is False
    assert summary["speculative_misses"] == []
    assert summary["misses"][0]["judge_latest"] == spoken


def test_an_unused_judgement_is_fatal_unless_its_text_was_only_ever_an_interim(
    tmp_path: Path,
) -> None:
    ledger = _ledger(
        tmp_path,
        _exchange(_window("Rani Twin, can"), "silent"),
        _exchange(_window("Rani Twin, raise your hand"), "raise_hand"),
    )

    summary: Any = ledger.summary(_heard(("Rani Twin, can",), ("Rani Twin, raise your hand",)))

    assert [item["judge_latest"] for item in summary["speculative_unused"]] == ["Rani Twin, can"]
    assert [item["judge_latest"] for item in summary["unused"]] == ["Rani Twin, raise your hand"]
    assert summary["closed"] is False


def test_a_request_that_is_not_a_judge_window_is_never_speculative() -> None:
    heard = _heard(("Twin, raise your hand",), ())

    assert heard.speculative(canonical_request(ROUTE, _body("Twin, raise your hand"))) is False


@pytest.mark.parametrize(
    ("recorded", "asked"),
    [
        ("The beta launch is 14 October 2026.", "The beta launch is 21 October 2026."),
        ("Slide 2 of 20", "Slide 20 of 20"),
        ("evidence ref 3f9a1c2b7d4e", "evidence ref 3f9a1c2b7d4f"),
        ("2026-11-19T18:48:17Z", "2026-11-19T18:48:18Z"),
    ],
)
def test_no_normalisation_hides_a_changed_value(tmp_path: Path, recorded: str, asked: str) -> None:
    ledger = _ledger(tmp_path, _exchange(_body(recorded), "recorded"))

    assert isinstance(ledger.respond(ROUTE, _body(asked)), ReplayMiss)


def _view(latency: int, seconds: int = 4) -> bytes:
    view = {"screen": {"title": "Plan", "vision_latency_ms": latency, "seconds_ago": seconds}}
    return _body("Meeting view: " + json.dumps(view))


def test_only_measured_latency_and_elapsed_seconds_are_treated_as_volatile(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path, _exchange(_view(2901), "described"), _exchange(_view(2901), "again"))

    jittered = ledger.respond(ROUTE, _view(2903))
    later = ledger.respond(ROUTE, _view(2901, seconds=5))
    retitled = ledger.respond(ROUTE, _view(2901).replace(b"Plan", b"Plot"))

    assert _text(jittered) == "described"
    assert _text(later) == "again"
    assert isinstance(retitled, ReplayMiss)
    assert [rule.name for rule in VOLATILE_RULES] == ["vision_latency_ms", "seconds_ago"]


def test_recordings_made_before_a_volatile_rule_still_match_under_it(tmp_path: Path) -> None:
    unruled = model_exchange(
        canonical_request(ROUTE, _view(2901), rules=()), 200, "application/json", _answer("seen")
    )
    ledger = _ledger(tmp_path, unruled)

    assert _text(ledger.respond(ROUTE, _view(3120))) == "seen"


def _structured_view(latency: int, frames: int = 1) -> bytes:
    response = {"screen": {"vision_latency_ms": latency, "frames": frames}}
    parts = [{"functionResponse": {"name": "meeting_view", "response": response}}]
    return json.dumps({"contents": [{"role": "user", "parts": parts}]}).encode()


def test_a_measured_value_is_volatile_as_a_structured_field_and_nothing_else_is(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path, _exchange(_structured_view(2888), "seen"))

    jittered = ledger.respond(ROUTE, _structured_view(2959))
    changed = ledger.respond(ROUTE, _structured_view(2888, frames=2))

    assert _text(jittered) == "seen"
    assert isinstance(changed, ReplayMiss)
