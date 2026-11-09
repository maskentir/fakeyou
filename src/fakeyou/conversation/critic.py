import asyncio
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.session.speech import BookEntry, ClaimHeader

_CRITIC_RULES = (
    "You are the critic of a digital twin in a meeting. You judge one contribution: the "
    "claims it is made of, the sources those claims cite and the words that carry them. You "
    "never rewrite the contribution and you never add to it.\n"
    "The brief and the owner model are the owner's own approved knowledge, and the room's "
    "own words are what the room said. Judge the contribution against those two things.\n"
    "owner_would_say: for an opinion or a proposal, true only when the owner would say this, "
    "in the owner's own voice, in this meeting; a view the owner does not hold, or words the "
    "owner would not choose, make it false. For a fact or a question, the cited source is "
    "the owner's own, so this is true unless the words say something the source does not "
    "carry.\n"
    "worth_interrupting: true only when the room needs this now. A remark nobody asked for "
    "that changes nothing for the discussion is not worth interrupting for.\n"
    "guesses_hedged: true when every guess is spoken as a guess. A guess stated as a fact, "
    "without a word that marks the uncertainty, makes it false. There is nothing to hedge "
    "when no claim is a guess.\n"
    "grounded: true only when every claim is supported by the cited sources and does not "
    "contradict what the room just said. A claim that goes beyond its sources, contradicts "
    "them, contradicts the room, or invents a detail is not grounded. When the room says "
    "something different from an owner item, saying the item as it stands is not grounded; "
    "saying it as the owner's view alongside what the room said is. A claim resting on an "
    "owner model item (basis owner_model) that repeats a date or number from that item "
    "after the room gave a different date or number for the same thing is not grounded, "
    "even when phrased as what the owner recorded, unless the contribution itself also "
    "gives the room's newer value as the room's. A claim resting on the brief (basis "
    "brief) is judged against the brief alone: the brief carries what the owner approved "
    "for this meeting, so a brief fact that differs from the room or the screen is "
    "grounded and is something the owner would say.\n"
    "adds_information: true only when the contribution tells the room something it does not "
    "already have. Restating the trigger, agreeing with it or thanking for it adds nothing.\n"
    "on_topic: true only when the contribution is about the subject the room discusses.\n"
    "confidence: how sure you are about your answers, from 0 to 1."
)


class CriticVerdict(BaseModel):
    model_config = ConfigDict(frozen=True)

    owner_would_say: bool = Field(description="The owner would say this in the owner's voice.")
    worth_interrupting: bool = Field(description="The room needs this now.")
    guesses_hedged: bool = Field(description="Every guess is spoken as a guess.")
    grounded: bool = Field(description="Every claim is supported by the cited sources.")
    adds_information: bool = Field(description="The contribution adds something the room lacks.")
    on_topic: bool = Field(description="The contribution is about the subject discussed.")
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class CriticClaim:
    header: ClaimHeader
    say: str


class ContributionCritic(Protocol):
    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict: ...


def refusal_reason(verdict: CriticVerdict, *, proactive: bool, threshold: float) -> str | None:
    if not verdict.grounded:
        return "not_grounded"
    if not verdict.on_topic:
        return "off_topic"
    if not verdict.owner_would_say:
        return "not_owner_would_say"
    if not verdict.guesses_hedged:
        return "guess_not_hedged"
    if proactive:
        if not verdict.adds_information:
            return "adds_nothing"
        if not verdict.worth_interrupting:
            return "not_worth_interrupting"
    if verdict.confidence < threshold:
        return "low_confidence"
    return None


class LazyCritic:
    def __init__(self, factory: Callable[[], ContributionCritic]) -> None:
        self._factory = factory
        self._critic: ContributionCritic | None = None

    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict:
        if self._critic is None:
            self._critic = self._factory()
        return await self._critic.judge(
            claims=claims,
            sources=sources,
            trigger_text=trigger_text,
            recent_lines=recent_lines,
        )


class ModelContributionCritic:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float) -> None:
        self._model = model
        self._bound: Runnable[Sequence[BaseMessage], object] | None = None
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(2)

    def _structured(self) -> Runnable[Sequence[BaseMessage], object]:
        if self._bound is None:
            self._bound = self._model.with_structured_output(CriticVerdict)
        return self._bound

    async def judge(
        self,
        *,
        claims: Sequence[CriticClaim],
        sources: Sequence[BookEntry],
        trigger_text: str,
        recent_lines: Sequence[str] = (),
    ) -> CriticVerdict:
        payload = json.dumps(
            {
                "claims": [
                    {
                        "act": claim.header.act,
                        "basis": claim.header.basis,
                        "refs": list(claim.header.refs),
                        "certainty": claim.header.certainty,
                        "speaks_as": claim.header.speaks_as,
                        "say": claim.say,
                    }
                    for claim in claims
                ],
                "cited_sources": [
                    {
                        "ref": source.fact_id,
                        "kind": source.kind,
                        "namespace": source.namespace,
                        "text": source.text,
                        "confidence": source.confidence,
                    }
                    for source in sources
                ],
                "trigger": trigger_text,
                "recent_room_lines": list(recent_lines),
            },
            ensure_ascii=False,
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=_CRITIC_RULES),
            HumanMessage(content=payload),
        ]
        async with self._slots:
            result = await asyncio.wait_for(
                self._structured().ainvoke(messages), timeout=self._timeout_seconds
            )
        return CriticVerdict.model_validate(result)
