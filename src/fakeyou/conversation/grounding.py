import asyncio
import json
from collections.abc import Sequence
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

_VERIFIER_RULES = (
    "You check one proactive contribution a digital twin wants to add to a meeting it was "
    "not asked to join in. You are given the contribution, the source text it cites, and "
    "what triggered it (a room line or a screen summary).\n"
    "grounded: true only when every factual claim in the contribution is supported by the "
    "cited source text. A claim that goes beyond the source, contradicts it, or invents a "
    "detail the source does not state is not grounded.\n"
    "adds_information: true only when the contribution tells the room something it does not "
    "already have from the trigger itself. Restating, paraphrasing, agreeing with, thanking "
    "for, or merely noticing the trigger adds nothing.\n"
    "on_topic: true only when the contribution is about the same subject as the trigger. A true "
    "fact from a source about something the room is not discussing is off topic, and off topic "
    "is noise rather than information.\n"
    "confidence: how sure you are about both answers, from 0 to 1."
)


class GroundingVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    grounded: bool = Field(description="Every claim is supported by the cited source text.")
    adds_information: bool = Field(
        description="The contribution tells the room something beyond the trigger."
    )
    on_topic: bool = Field(description="The contribution is about the same subject as the trigger.")
    confidence: float = Field(ge=0.0, le=1.0)


class GroundingVerifier(Protocol):
    async def verify(
        self, *, text: str, source_text: str, trigger_text: str, kind: str | None
    ) -> GroundingVerdict: ...


class ModelGroundingVerifier:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float) -> None:
        self._model = model
        self._bound: Runnable[Sequence[BaseMessage], object] | None = None
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(2)

    def _structured(self) -> Runnable[Sequence[BaseMessage], object]:
        if self._bound is None:
            self._bound = self._model.with_structured_output(GroundingVerdict)
        return self._bound

    async def verify(
        self, *, text: str, source_text: str, trigger_text: str, kind: str | None
    ) -> GroundingVerdict:
        payload = json.dumps(
            {
                "contribution_kind": kind,
                "contribution": text,
                "cited_source": source_text,
                "trigger": trigger_text,
            },
            ensure_ascii=False,
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=_VERIFIER_RULES),
            HumanMessage(content=payload),
        ]
        async with self._slots:
            result = await asyncio.wait_for(
                self._structured().ainvoke(messages), timeout=self._timeout_seconds
            )
        return GroundingVerdict.model_validate(result)
