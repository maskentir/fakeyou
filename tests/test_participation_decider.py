import pytest

from fakeyou.conversation.participation_decider import (
    GeminiDecider,
    JevDecider,
    ParticipationDecider,
    build_gemini_payload,
    build_jev_payload,
    parse_gemini_response,
    parse_jev_response,
)


async def _fake_transport(payload: dict) -> dict:
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {
        "decision",
        "addressed_to_me",
        "others_conversing",
        "needs_clarify",
    }
    return {
        "decision": {
            "choice": "speak",
            "confidence": 0.82,
            "probs": {"speak": 0.82, "silent": 0.12, "clarify": 0.06},
        },
        "addressed_to_me": {"probability": 0.9},
        "others_conversing": {"probability": 0.05},
        "needs_clarify": {"probability": 0.1},
    }


def test_payload_contains_parallel_questions():
    payload = build_jev_payload("Harun, you're up", "jev-latest")
    assert payload["questions"]["decision"]["type"] == "choice"


def test_parse_maps_choice_and_confidence():
    body = {
        "decision": {"choice": "clarify", "confidence": 0.61, "probs": {}},
        "addressed_to_me": {"probability": 0.4},
        "others_conversing": {"probability": 0.4},
        "needs_clarify": {"probability": 0.66},
    }
    decision = parse_jev_response(body)
    assert (decision.action, decision.confidence) == ("clarify", 0.61)


@pytest.mark.asyncio
async def test_decider_uses_injected_transport():
    decider = JevDecider(
        api_key="k",
        model="jev-latest",
        base_url="https://x",
        timeout_seconds=2.0,
        transport=_fake_transport,
    )
    decision = await decider.decide("state")
    assert decision.action == "speak"
    await decider.aclose()


@pytest.mark.asyncio
async def test_transport_error_falls_back_silent():
    async def _boom(_payload: dict) -> dict:
        raise TimeoutError("jev down")

    decider = JevDecider(
        api_key="k",
        model="jev-latest",
        base_url="https://x",
        timeout_seconds=2.0,
        transport=_boom,
    )
    decision = await decider.decide("state")
    assert (decision.action, decision.confidence) == ("silent", 0.0)
    await decider.aclose()


def _gemini_body(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def test_gemini_payload_targets_generate_content():
    path, payload = build_gemini_payload("a: hello", "gemini-3.8-flash")
    assert path == "gemini-3.8-flash:generateContent"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"


def test_gemini_parse_maps_decision_and_confidence():
    body = _gemini_body(
        '{"decision": "clarify", "confidence": 0.66, "addressed_to_me": 0.4, '
        '"others_conversing": 0.4, "needs_clarify": 0.7}'
    )
    decision = parse_gemini_response(body)
    assert (decision.action, decision.confidence) == ("clarify", 0.66)


def test_gemini_parse_rejects_garbage():
    assert parse_gemini_response({}).action == "silent"
    assert parse_gemini_response(_gemini_body("not json")).action == "silent"
    assert parse_gemini_response(_gemini_body('{"decision": "dance"}')).action == "silent"


@pytest.mark.asyncio
async def test_gemini_decider_uses_injected_transport():
    async def _ok(_payload: dict) -> dict:
        return _gemini_body('{"decision": "speak", "confidence": 0.81}')

    decider = GeminiDecider(
        api_key="k",
        model="gemini-3.8-flash",
        base_url="https://generativelanguage.googleapis.com",
        timeout_seconds=2.0,
        transport=_ok,
    )
    assert isinstance(decider, ParticipationDecider)
    decision = await decider.decide("state")
    assert (decision.action, decision.confidence) == ("speak", 0.81)
    await decider.aclose()


@pytest.mark.asyncio
async def test_both_deciders_satisfy_the_contract():
    async def _speak(_payload: dict) -> dict:
        return {
            "decision": {"choice": "speak", "confidence": 0.9, "probs": {}},
            "addressed_to_me": {"probability": 0.9},
            "others_conversing": {"probability": 0.0},
            "needs_clarify": {"probability": 0.0},
        }

    async def _silent(_payload: dict) -> dict:
        return _gemini_body('{"decision": "silent", "confidence": 0.95}')

    deciders: list[ParticipationDecider] = [
        JevDecider(
            api_key="k",
            model="jev-latest",
            base_url="https://x",
            timeout_seconds=2.0,
            transport=_speak,
        ),
        GeminiDecider(
            api_key="k",
            model="gemini-3.8-flash",
            base_url="https://y",
            timeout_seconds=2.0,
            transport=_silent,
        ),
    ]
    for decider in deciders:
        assert isinstance(decider, ParticipationDecider)
        decision = await decider.decide("state")
        assert decision.action in ("speak", "silent", "clarify")
        await decider.aclose()
