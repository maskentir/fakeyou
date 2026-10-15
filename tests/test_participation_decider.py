import pytest
from fakeyou.conversation.participation_decider import JevDecider, build_jev_payload, parse_jev_response


async def _fake_transport(payload: dict) -> dict:
    assert payload["model"] == "jev-latest"
    assert set(payload["questions"]) == {"decision", "addressed_to_me", "others_conversing", "needs_clarify"}
    return {
        "decision": {"choice": "speak", "confidence": 0.82, "probs": {"speak": 0.82, "silent": 0.12, "clarify": 0.06}},
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
    decider = JevDecider(api_key="k", model="jev-latest", base_url="https://x", timeout_seconds=2.0, transport=_fake_transport)
    decision = await decider.decide("state")
    assert decision.action == "speak"
    await decider.aclose()


@pytest.mark.asyncio
async def test_transport_error_falls_back_silent():
    async def _boom(payload: dict) -> dict:
        raise TimeoutError("jev down")
    decider = JevDecider(api_key="k", model="jev-latest", base_url="https://x", timeout_seconds=2.0, transport=_boom)
    decision = await decider.decide("state")
    assert (decision.action, decision.confidence) == ("silent", 0.0)
    await decider.aclose()
