import json
from collections.abc import Sequence
from typing import Literal, Protocol, TypedDict

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field


class ParticipationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Literal["speak", "silent", "clarify"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1)


class DeciderTurn(TypedDict):
    speaker: str
    text: str
    mine: bool


class Decider(Protocol):
    async def decide(self, window: Sequence[DeciderTurn]) -> ParticipationDecision: ...


class UnsupportedDeciderProvider(Exception):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(
            f"participation decider provider has no structured output path: {provider}"
        )


_PARTICIPATION_ACTIONS: dict[str, Literal["reply", "silent", "clarify"]] = {
    "speak": "reply",
    "silent": "silent",
    "clarify": "clarify",
}


def to_participation_action(
    decision: ParticipationDecision,
) -> Literal["reply", "silent", "clarify"]:
    return _PARTICIPATION_ACTIONS[decision.decision]


class StructuredRunnable(Protocol):
    async def ainvoke(self, input: Sequence[BaseMessage], /) -> object: ...


class StructuredChatModel(Protocol):
    def with_structured_output(self, schema: type[ParticipationDecision]) -> StructuredRunnable: ...


_DECIDER_RULES = (
    "Decide whether the twin joins the floor of a live meeting. "
    "The recent turns arrive as JSON with speaker, text and mine. "
    "mine is true on turns that call the twin by name. "
    "Answer speak when the last turn is addressed to the twin, "
    "clarify when the target is unclear and nobody else holds the floor, "
    "and silent in all other cases, including talk between two other people. "
    "Give a confidence between 0 and 1 and a short snake_case reason_code."
)


def _render_window(window: Sequence[DeciderTurn]) -> str:
    return json.dumps(list(window), ensure_ascii=False)


class StructuredGeminiDecider:
    def __init__(self, *, model: StructuredChatModel) -> None:
        self._structured = model.with_structured_output(ParticipationDecision)

    async def decide(self, window: Sequence[DeciderTurn]) -> ParticipationDecision:
        messages: list[BaseMessage] = [
            SystemMessage(content=_DECIDER_RULES),
            HumanMessage(content=_render_window(window)),
        ]
        result = await self._structured.ainvoke(messages)
        return ParticipationDecision.model_validate(result)
