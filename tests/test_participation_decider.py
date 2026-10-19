from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.messages import BaseMessage
from pydantic import SecretStr, ValidationError

from fakeyou.config.settings import ParticipationDeciderSettings
from fakeyou.conversation.participation_decider import (
    DeciderTurn,
    ParticipationDecision,
    StructuredGeminiDecider,
    UnsupportedDeciderProvider,
    to_participation_action,
)
from fakeyou.conversation.participation_graph import DeciderFailure
from fakeyou.orchestration.assembly import build_decider


class FakeStructuredRunnable:
    def __init__(
        self, result: ParticipationDecision, received: list[Sequence[BaseMessage]]
    ) -> None:
        self._result = result
        self._received = received

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> ParticipationDecision:
        self._received.append(messages)
        return self._result


class FakeStructuredModel:
    def __init__(self, result: ParticipationDecision) -> None:
        self.schemas: list[Any] = []
        self.received: list[Sequence[BaseMessage]] = []
        self._result = result

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> FakeStructuredRunnable:
        self.schemas.append(schema)
        return FakeStructuredRunnable(self._result, self.received)


def decider_settings_with_key() -> ParticipationDeciderSettings:
    return ParticipationDeciderSettings(
        provider="jev", base_url="https://jev.example", api_key=SecretStr("test-key")
    )


@pytest.mark.asyncio
async def test_gemini_decider_returns_contract_schema() -> None:
    expected = ParticipationDecision(decision="speak", confidence=0.9, reason_code="addressed")
    model = FakeStructuredModel(expected)
    decider = StructuredGeminiDecider(model=model)
    out = await decider.decide([{"speaker": "Ana", "text": "Twin, agree?", "mine": False}])
    assert out == ParticipationDecision(decision="speak", confidence=0.9, reason_code="addressed")
    assert to_participation_action(out) == "reply"


@pytest.mark.asyncio
async def test_gemini_decider_requests_schema_and_sends_window_turns() -> None:
    model = FakeStructuredModel(
        ParticipationDecision(decision="silent", confidence=0.4, reason_code="not_addressed")
    )
    decider = StructuredGeminiDecider(model=model)
    window: list[DeciderTurn] = [
        {"speaker": "Ana", "text": "Twin, agree?", "mine": True},
        {"speaker": "Budi", "text": "Okay", "mine": False},
    ]
    await decider.decide(window)
    assert model.schemas == [ParticipationDecision]
    rendered = " ".join(str(message.content) for message in model.received[0])
    assert "Twin, agree?" in rendered
    assert "Okay" in rendered


def test_decision_schema_rejects_out_of_range_confidence() -> None:
    with pytest.raises(ValidationError):
        ParticipationDecision(decision="speak", confidence=1.5, reason_code="x")
    with pytest.raises(ValidationError):
        ParticipationDecision.model_validate(
            {"decision": "maybe", "confidence": 0.5, "reason_code": "x"}
        )


def test_participation_action_maps_every_decision() -> None:
    assert (
        to_participation_action(
            ParticipationDecision(decision="speak", confidence=0.7, reason_code="a")
        )
        == "reply"
    )
    assert (
        to_participation_action(
            ParticipationDecision(decision="silent", confidence=0.7, reason_code="a")
        )
        == "silent"
    )
    assert (
        to_participation_action(
            ParticipationDecision(decision="clarify", confidence=0.7, reason_code="a")
        )
        == "clarify"
    )


def test_jev_is_not_a_decision_path() -> None:
    with pytest.raises(UnsupportedDeciderProvider, match="jev"):
        build_decider(provider="jev", settings=decider_settings_with_key())


def test_gemini_decider_without_credentials_fails_at_construction() -> None:
    with pytest.raises(DeciderFailure, match="decider_no_credentials"):
        build_decider(provider="gemini", settings=ParticipationDeciderSettings(provider="gemini"))
