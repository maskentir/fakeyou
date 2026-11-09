import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass
from functools import partial
from typing import Literal

from fakeyou.conversation.reply_planner import IntentHandler, ReplyIntent, SpokenChunk
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import JournalSink

DropReason = Literal["key_mismatch", "not_admitted", "not_ready", "superseded", "unused", "closed"]
DRAFT_REQUEST = "draft"


@dataclass(frozen=True, slots=True)
class DraftKey:
    transcript_ids: tuple[str, ...]
    request: str
    evidence_needs: tuple[str, ...]
    owner_claim: bool
    history: int

    @classmethod
    def of(
        cls, batch: tuple[TranscriptEvent, ...], reply_event: TranscriptEvent, *, history: int
    ) -> "DraftKey":
        return cls(
            tuple(event.id for event in batch),
            reply_event.text,
            tuple(reply_event.evidence_needs),
            reply_event.owner_claim,
            history,
        )


ANSWERED_LIVE: frozenset[ReplyIntent] = frozenset({"repeat"})


class HeldEffects:
    def __init__(self) -> None:
        self._intent: IntentHandler | None = None
        self._language: Callable[[str], None] | None = None
        self._held: list[Callable[[], object]] = []

    def bind(self, intent: IntentHandler, language: Callable[[str], None]) -> None:
        self._intent = intent
        self._language = language

    def intent(self, intent: ReplyIntent) -> str | None:
        if self._intent is None:
            return None
        if intent in ANSWERED_LIVE:
            return self._intent(intent)
        self._held.append(partial(self._intent, intent))
        return None

    def language(self, language: str) -> None:
        if self._language is not None:
            self._held.append(partial(self._language, language))

    def release(self) -> None:
        held, self._held = self._held, []
        for effect in held:
            effect()


@dataclass(frozen=True, slots=True)
class DraftRequest:
    key: DraftKey
    request_id: str
    chunks: AsyncIterator[SpokenChunk]
    effects: HeldEffects
    tool: "ToolUse"


@dataclass(slots=True)
class ToolUse:
    started: bool = False
    listener: Callable[[], None] | None = None

    def start(self) -> None:
        self.started = True
        if self.listener is not None:
            self.listener()

    def forward(self, listener: Callable[[], None]) -> None:
        self.listener = listener
        if self.started:
            listener()


class ReplyDraft:
    def __init__(self, request: DraftRequest) -> None:
        self.key = request.key
        self.request_id = request.request_id
        self._effects = request.effects
        self._tool = request.tool
        self._chunks: list[SpokenChunk] = []
        self._changed = asyncio.Condition()
        self._finished = False
        self._error: Exception | None = None
        self._started = time.monotonic()
        self._task = asyncio.ensure_future(self._fill(request.chunks))

    @property
    def buffered(self) -> int:
        return len(self._chunks)

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self._started) * 1000.0

    def accept(self, on_tool_start: Callable[[], None]) -> None:
        self._effects.release()
        self._tool.forward(on_tool_start)

    async def _fill(self, chunks: AsyncIterator[SpokenChunk]) -> None:
        try:
            _, self._error = await capture_error(partial(self._collect, chunks))
        finally:
            async with self._changed:
                self._finished = True
                self._changed.notify_all()

    async def _collect(self, chunks: AsyncIterator[SpokenChunk]) -> None:
        async for chunk in chunks:
            async with self._changed:
                self._chunks.append(chunk)
                self._changed.notify_all()

    def _ready(self, index: int) -> bool:
        return index < len(self._chunks) or self._finished

    async def replay(self) -> AsyncGenerator[SpokenChunk]:
        index = 0
        try:
            while True:
                async with self._changed:
                    await self._changed.wait_for(partial(self._ready, index))
                    ready = self._chunks[index:]
                    finished = self._finished
                for chunk in ready:
                    index += 1
                    yield chunk
                if finished and index >= len(self._chunks):
                    if self._error is not None:
                        raise self._error
                    return
        finally:
            await self.cancel()

    async def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class DraftResponder:
    def __init__(self, draft: ReplyDraft) -> None:
        self._draft = draft

    def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        return self._draft.replay()


DraftOpener = Callable[[], Awaitable[DraftRequest | None]]


@dataclass(slots=True)
class _Slot:
    transcript_id: str
    opening: asyncio.Task[DraftRequest | None]
    draft: ReplyDraft | None = None
    retired: bool = False


class ReplyDrafts:
    def __init__(self, journal: JournalSink) -> None:
        self._journal = journal
        self._slot: _Slot | None = None
        self._cancelling: set[asyncio.Task[None]] = set()

    def start(self, transcript_id: str, opener: DraftOpener) -> None:
        self._retire("superseded")
        slot = _Slot(transcript_id, asyncio.ensure_future(opener()))
        slot.opening.add_done_callback(partial(self._opened, slot))
        self._slot = slot

    def _opened(self, slot: _Slot, task: asyncio.Task[DraftRequest | None]) -> None:
        if slot.retired or task.cancelled() or task.exception() is not None:
            return
        request = task.result()
        if request is None:
            return
        slot.draft = ReplyDraft(request)
        self._journal.record(
            "reply_draft_started",
            source="agent",
            transcript_id=slot.transcript_id,
            request_id=request.request_id,
            evidence_needs=list(request.key.evidence_needs),
        )

    def take(self, key: DraftKey) -> ReplyDraft | None:
        slot = self._slot
        if slot is None or slot.transcript_id not in key.transcript_ids:
            return None
        self._slot = None
        slot.retired = True
        draft = slot.draft
        if draft is None:
            self._dispose(slot, "not_ready")
            return None
        if draft.key != key:
            self._dispose(slot, "key_mismatch")
            return None
        self._journal.record(
            "reply_draft_accepted",
            source="agent",
            transcript_id=slot.transcript_id,
            request_id=draft.request_id,
            buffered=draft.buffered,
            head_start_ms=round(draft.age_ms, 1),
        )
        return draft

    def drop(self, transcript_ids: Collection[str], reason: DropReason) -> None:
        slot = self._slot
        if slot is not None and slot.transcript_id in transcript_ids:
            self._retire(reason)

    async def close(self) -> None:
        slot = self._slot
        self._retire("closed")
        if slot is not None:
            await asyncio.gather(slot.opening, return_exceptions=True)
            if slot.draft is not None:
                await slot.draft.cancel()
        await asyncio.gather(*self._cancelling, return_exceptions=True)

    def _retire(self, reason: DropReason) -> None:
        slot = self._slot
        if slot is None:
            return
        self._slot = None
        slot.retired = True
        self._dispose(slot, reason)

    def _dispose(self, slot: _Slot, reason: DropReason) -> None:
        slot.opening.cancel()
        if slot.draft is not None:
            cancelling = asyncio.ensure_future(slot.draft.cancel())
            self._cancelling.add(cancelling)
            cancelling.add_done_callback(self._cancelling.discard)
        self._journal.record(
            "reply_draft_dropped",
            source="agent",
            transcript_id=slot.transcript_id,
            request_id=slot.draft.request_id if slot.draft is not None else None,
            reason=reason,
        )
