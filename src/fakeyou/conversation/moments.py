import asyncio
import json
import math
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.conversation.contribution import SourceRegistry
from fakeyou.foundation.participation import mentions_whole_phrase
from fakeyou.foundation.transcript import AttentionContext, TranscriptEvent
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal

MomentKind = Literal[
    "none", "unanswered_question", "owner_task", "decision_vs_brief", "fact_opportunity"
]
EmbedTexts = Callable[[Sequence[str]], Awaitable[list[list[float]]]]
_MAX_CANDIDATES = 3
_WINDOW_LINES = 8

_JUDGE_RULES = (
    "You watch a live meeting for a digital twin that nobody has addressed. Decide whether "
    "the latest line creates a moment where the twin, speaking for its owner, has something "
    "the room does not have. You are given recent lines and candidate source items (owner "
    "profile facts and meeting brief items) that are semantically close to the latest line.\n"
    "moment: unanswered_question when the latest line asks the room something a candidate "
    "source can answer and nobody has answered yet; owner_task only when the room assigns "
    "or expects a concrete action from the owner, not merely when the topic touches "
    "something the owner owns; decision_vs_brief when the room settles on a value, date or "
    "plan that conflicts with what a candidate source states, whichever source it is; "
    "fact_opportunity when the discussion would materially benefit from a fact in a "
    "candidate source that the room has not stated; none otherwise. A line addressed "
    "to the twin by name is never a moment: the twin answers those directly.\n"
    "source_ref: the id of the candidate source item the moment rests on, copied exactly, "
    "or null when the moment is none.\n"
    "confidence: how sure you are that interrupting here helps rather than annoys, 0 to 1. "
    "Most lines are not moments; when in doubt choose none."
)


class MomentJudgement(BaseModel):
    model_config = ConfigDict(frozen=True)

    moment: MomentKind
    source_ref: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)


@dataclass(frozen=True, slots=True)
class MomentCandidate:
    source: str
    ref: str
    text: str
    similarity: float


class MomentJudge(Protocol):
    async def judge(
        self,
        *,
        lines: Sequence[tuple[str, str]],
        candidates: Sequence[MomentCandidate],
        owner_names: Sequence[str],
        twin_names: Sequence[str],
    ) -> MomentJudgement: ...


class ModelMomentJudge:
    def __init__(self, model: BaseChatModel, *, timeout_seconds: float) -> None:
        self._model = model
        self._bound: Runnable[Sequence[BaseMessage], object] | None = None
        self._timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(2)

    def _structured(self) -> Runnable[Sequence[BaseMessage], object]:
        if self._bound is None:
            self._bound = self._model.with_structured_output(MomentJudgement)
        return self._bound

    async def judge(
        self,
        *,
        lines: Sequence[tuple[str, str]],
        candidates: Sequence[MomentCandidate],
        owner_names: Sequence[str],
        twin_names: Sequence[str],
    ) -> MomentJudgement:
        payload = json.dumps(
            {
                "twin_names": list(twin_names),
                "owner_names": list(owner_names),
                "lines": [{"speaker": speaker, "text": text} for speaker, text in lines],
                "candidate_sources": [
                    {"source": c.source, "id": c.ref, "text": c.text} for c in candidates
                ],
            },
            ensure_ascii=False,
        )
        messages: list[BaseMessage] = [
            SystemMessage(content=_JUDGE_RULES),
            HumanMessage(content=payload),
        ]
        async with self._slots:
            result = await asyncio.wait_for(
                self._structured().ainvoke(messages), timeout=self._timeout_seconds
            )
        return MomentJudgement.model_validate(result)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


@dataclass(slots=True)
class _Pending:
    context: AttentionContext
    speaker_id: str
    asked_at: float


@dataclass(slots=True)
class MomentDetector:
    registry: SourceRegistry
    judge: MomentJudge
    embed: EmbedTexts | None = None
    twin_names: tuple[str, ...] = ()
    owner_names: tuple[str, ...] = ()
    similarity_threshold: float = 0.15
    moment_confidence: float = 0.8
    unanswered_seconds: float = 15.0
    journal: Journal | None = None
    _vectors: list[tuple[str, str, str, list[float]]] = field(default_factory=list, init=False)
    _prepared: bool = field(default=False, init=False)
    _recent: deque[tuple[str, str]] = field(
        default_factory=lambda: deque(maxlen=_WINDOW_LINES), init=False
    )
    _pending: _Pending | None = field(default=None, init=False)
    _ready: deque[AttentionContext] = field(default_factory=deque, init=False)
    _fired_events: set[str] = field(default_factory=set, init=False)
    _fired_refs: set[str] = field(default_factory=set, init=False)

    async def prepare(self) -> None:
        if self._prepared:
            return
        self._prepared = True
        items = self.registry.items()
        if self.embed is None or not items:
            self._note("moments_disabled", reason="no_embedder" if items else "no_sources")
            return
        embed = self.embed
        assert embed is not None
        vectors, error = await capture_error(partial(embed, [text for _, _, text in items]))
        if vectors is None or error is not None or len(vectors) != len(items):
            self._note("moments_disabled", reason="embed_failed")
            self.embed = None
            return
        self._vectors = [
            (source, ref, text, vector)
            for (source, ref, text), vector in zip(items, vectors, strict=True)
        ]

    def _note(self, event: str, **fields: object) -> None:
        if self.journal is not None:
            self.journal.record(event, source="agent", **fields)

    async def _candidates(self, text: str) -> list[MomentCandidate]:
        if self.embed is None or not self._vectors:
            return []
        vectors, error = await capture_error(partial(self.embed, [text]))
        if vectors is None or error is not None or not vectors:
            return []
        scored = [
            MomentCandidate(source=source, ref=ref, text=item_text, similarity=score)
            for source, ref, item_text, vector in self._vectors
            if (score := _cosine(vectors[0], vector)) >= self.similarity_threshold
        ]
        scored.sort(key=lambda candidate: candidate.similarity, reverse=True)
        return scored[:_MAX_CANDIDATES]

    def _names_twin(self, text: str) -> bool:
        return any(mentions_whole_phrase(text, name) for name in self.twin_names)

    def _passes_gate(self, event: TranscriptEvent) -> bool:
        if not event.finalized or not event.text.strip():
            return False
        if self._names_twin(event.text):
            self._pending = None
            return False
        return event.id not in self._fired_events

    async def observe(self, event: TranscriptEvent, *, now: float) -> AttentionContext | None:
        self._recent.append((event.participant_id, event.text))
        if self._pending is not None and event.participant_id != self._pending.speaker_id:
            self._pending = None
        if not self._passes_gate(event):
            return None
        candidates = await self._candidates(event.text)
        if not candidates:
            return None
        judgement, error = await capture_error(
            partial(
                self.judge.judge,
                lines=tuple(self._recent),
                candidates=candidates,
                owner_names=self.owner_names,
                twin_names=self.twin_names,
            )
        )
        if judgement is None or error is not None or judgement.moment == "none":
            return None
        if judgement.confidence < self.moment_confidence:
            return None
        ref = judgement.source_ref
        if ref is None or ref not in {candidate.ref for candidate in candidates}:
            return None
        if ref in self._fired_refs:
            return None
        candidate = next(item for item in candidates if item.ref == ref)
        context = AttentionContext(
            kind=judgement.moment,
            trigger_text=event.text,
            trigger_event_id=event.id,
            suggested_source_ref=ref,
            suggested_source=candidate.source,
            source_text=candidate.text,
        )
        self._note(
            "moment_detected",
            kind=context.kind,
            source_ref=ref,
            confidence=judgement.confidence,
            transcript_id=event.id,
        )
        if judgement.moment == "unanswered_question":
            self._pending = _Pending(context=context, speaker_id=event.participant_id, asked_at=now)
            return None
        self._fired_events.add(event.id)
        self._fired_refs.add(ref)
        self._ready.append(context)
        return context

    def take_ready(self, *, now: float) -> AttentionContext | None:
        if self._ready:
            return self._ready.popleft()
        return self.poll(now=now)

    def poll(self, *, now: float) -> AttentionContext | None:
        pending = self._pending
        if pending is None or now - pending.asked_at < self.unanswered_seconds:
            return None
        self._pending = None
        context = pending.context
        if context.trigger_event_id is not None:
            self._fired_events.add(context.trigger_event_id)
        if context.suggested_source_ref is not None:
            self._fired_refs.add(context.suggested_source_ref)
        return context
