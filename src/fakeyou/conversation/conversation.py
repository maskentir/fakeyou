import hashlib
import json
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Protocol, override, runtime_checkable
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler, BaseCallbackManager
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable, RunnableConfig

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.critic import (
    ContributionCritic,
    CriticClaim,
    CriticVerdict,
    refusal_reason,
)
from fakeyou.conversation.meeting_messages import (
    MeetingHistory,
    history_messages,
    last_twin_text,
    recent_room_lines,
    request_messages,
)
from fakeyou.conversation.middleware import SPEECH_MARK, SpeechMark
from fakeyou.conversation.reply_draft import HeldEffects
from fakeyou.conversation.reply_planner import (
    IntentHandler,
    PlanRead,
    ReplyHandlers,
    ReplyIntent,
    ReplyPlanner,
    SpeechGate,
    SpeechPiece,
    SpokenChunk,
    TurnLanguage,
)
from fakeyou.conversation.sentence_stream import SentenceStream
from fakeyou.conversation.speech_book import BookSource, TurnEvidence, fact_book
from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.sentences import split_sentences
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import ConsumerSink, capture_error, guard_consumer
from fakeyou.observability.journal import Journal, JournalTiming
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.session.speech import (
    OWNER_VIEWS,
    BookEntry,
    ClaimHeader,
    FactBook,
    SpeechVerdict,
)


@runtime_checkable
class ConversationResponder(Protocol):
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk: ...


@runtime_checkable
class StreamingConversationResponder(Protocol):
    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
        effects: HeldEffects | None = None,
    ) -> AsyncIterator[SpokenChunk]: ...


MeetingAgent = Runnable[dict[str, list[BaseMessage]], dict[str, Any]]
REPLY_CONFIG: RunnableConfig = {"recursion_limit": 30}


def _tool_result_code(text: str) -> str | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, dict):
        code = parsed.get("code")
        if isinstance(code, str):
            return code
    return None


class _ToolActivity(AsyncCallbackHandler):
    raise_error = True

    def __init__(
        self,
        on_start: Callable[[], None],
        journal: Journal | None,
        *,
        transcript_id: str | None = None,
    ) -> None:
        self._on_start = on_start
        self._journal = journal
        self._transcript_id = transcript_id
        self._running: dict[
            UUID, tuple[str, AbstractContextManager[JournalTiming], JournalTiming]
        ] = {}

    @override
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        if self._journal is not None:
            self._journal.request_started(
                str(run_id), source="llm", operation="chat", transcript_id=self._transcript_id
            )

    @override
    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_finished(
                str(run_id), source="llm", transcript_id=self._transcript_id
            )

    @override
    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_failed(
                str(run_id),
                source="llm",
                transcript_id=self._transcript_id,
                error=type(error).__name__,
            )

    @override
    async def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        journal = self._journal or Journal("", echo=False)
        context = journal.timed(
            started="tool_started",
            finished="tool_finished",
            failed="tool_failed",
            source="agent",
            tool=str(name),
            transcript_id=self._transcript_id,
        )
        self._running[run_id] = (str(name), context, context.__enter__())
        self._on_start()

    @override
    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        text = output.text if isinstance(output, BaseMessage) else str(output)
        _name, context, timing = self._running.pop(run_id)
        timing.fields["output_lines"] = len(text.splitlines())
        code = _tool_result_code(text)
        if code is not None:
            timing.fields["result_code"] = code
        context.__exit__(None, None, None)

    @override
    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        _, context, _ = self._running.pop(run_id)
        context.__exit__(type(error), error, error.__traceback__)


class _StreamPhase(_ToolActivity):
    def __init__(
        self,
        on_start: Callable[[], None],
        journal: Journal | None,
        *,
        transcript_id: str | None = None,
    ) -> None:
        super().__init__(on_start, journal, transcript_id=transcript_id)
        self.pending = 0
        self.boundary = 0
        self._counted: set[UUID] = set()

    @override
    async def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        await super().on_tool_start(serialized, input_str, run_id=run_id, **kwargs)
        self.pending += 1
        self.boundary += 1
        self._counted.add(run_id)

    @override
    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            await super().on_tool_end(output, run_id=run_id, **kwargs)
        finally:
            if run_id in self._counted:
                self._counted.discard(run_id)
                self.pending -= 1
            self.boundary += 1

    @override
    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            await super().on_tool_error(error, run_id=run_id, **kwargs)
        finally:
            if run_id in self._counted:
                self._counted.discard(run_id)
                self.pending -= 1
            self.boundary += 1


class _SentenceStream(SentenceStream):
    def __init__(
        self,
        request_id: str,
        transcript_id: str | None,
        max_sentences: int,
        language: str,
        journal: Journal | None,
    ) -> None:
        super().__init__(max_sentences, language)
        self.request_id = request_id
        self.transcript_id = transcript_id
        self.journal = journal
        self.spoken_chars = 0
        self.observed_chars = 0
        self.first_text = False
        self.claim = 0

    def note_text(self, piece: str) -> None:
        self.observed_chars += len(piece)
        if not self.first_text and (self.buffer + piece).strip():
            self.first_text = True
            if self.journal is not None:
                self.journal.record(
                    "answer_text_emitted",
                    source="llm",
                    request_id=self.request_id,
                    transcript_id=self.transcript_id,
                )

    def _count(self, sentence: str, index: int) -> None:
        self.spoken_chars += len(sentence) + (1 if self.spoken_chars else 0)
        if self.journal is not None:
            self.journal.record(
                "answer_sentence_emitted",
                source="agent",
                request_id=self.request_id,
                transcript_id=self.transcript_id,
                sentence_index=index,
                claim_index=self.claim,
                chars=len(sentence),
            )

    @override
    def take(self, piece: str) -> list[str]:
        start = self.yielded
        sentences = super().take(piece)
        for offset, sentence in enumerate(sentences):
            self._count(sentence, start + offset)
        return sentences

    @override
    def flush_segment(self) -> str | None:
        tail = super().flush_segment()
        if tail is not None:
            self._count(tail, self.yielded - 1)
        return tail

    @override
    def check_cap(self) -> bool:
        capped = super().check_cap()
        if capped and self.journal is not None:
            self.journal.record(
                "reply_capped",
                source="agent",
                request_id=self.request_id,
                chars_in=self.observed_chars,
                chars_out=self.spoken_chars,
            )
        return capped


def _filter_piece(
    message: Any,
    metadata: Any,
    state: _SentenceStream,
    phase: _StreamPhase,
    snapshot: int,
) -> tuple[str, int, bool]:
    if not isinstance(metadata, dict) or metadata.get("langgraph_node") != "model":
        return "", snapshot, False
    if phase.boundary != snapshot:
        snapshot = phase.boundary
        state.buffer = ""
    if phase.pending:
        return "", snapshot, False
    piece = message.text if isinstance(message, BaseMessage) else ""
    if not piece:
        return "", snapshot, False
    return piece, snapshot, True


async def _close_stream_optional(stream: AsyncIterator[Any], journal: Journal | None) -> None:
    if isinstance(stream, SupportsAclose):
        await guard_consumer(
            stream.aclose, sink=ConsumerSink(journal=journal), source="agent", detail="close_stream"
        )


def _ignore_tool_start() -> None:
    return None


@dataclass(frozen=True, slots=True)
class CriticOutcome:
    verdict: CriticVerdict | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class ClaimReview:
    critic: ContributionCritic
    gate: SpeechGate
    threshold: float
    proactive: bool
    trigger_text: str
    recent_lines: tuple[str, ...]
    journal: Journal | None
    request_id: str
    transcript_id: str | None

    async def judge(self, header: ClaimHeader, say: str) -> bool:
        outcome = await self.judge_plan([CriticClaim(header, say)])
        return outcome.reason is None

    async def judge_plan(self, claims: Sequence[CriticClaim]) -> CriticOutcome:
        started = monotonic()
        checked, _error = await capture_error(
            partial(
                self.critic.judge,
                claims=claims,
                sources=self._sources(claims),
                trigger_text=self.trigger_text,
                recent_lines=self.recent_lines,
            )
        )
        elapsed_ms = (monotonic() - started) * 1000
        if checked is None:
            return self._outcome(claims, None, "critic_failed", elapsed_ms)
        reason = refusal_reason(checked, proactive=self.proactive, threshold=self.threshold)
        return self._outcome(claims, checked, reason, elapsed_ms)

    def _sources(self, claims: Sequence[CriticClaim]) -> list[BookEntry]:
        entries = self.gate.book.entries
        refs = list(dict.fromkeys(ref for claim in claims for ref in claim.header.refs))
        return [entries[ref] for ref in refs if ref in entries]

    def _outcome(
        self,
        claims: Sequence[CriticClaim],
        verdict: CriticVerdict | None,
        reason: str | None,
        elapsed_ms: float,
    ) -> CriticOutcome:
        if self.journal is not None:
            self.journal.record(
                "critic_decided",
                source="agent",
                request_id=self.request_id,
                transcript_id=self.transcript_id,
                claims=[
                    {**claim.header.model_dump(mode="json"), "say": claim.say} for claim in claims
                ],
                refused=reason is not None,
                reason=reason,
                duration_ms=elapsed_ms,
                verdict=verdict.model_dump(mode="json") if verdict is not None else None,
            )
        return CriticOutcome(verdict=verdict, reason=reason)


@dataclass(frozen=True, slots=True)
class StreamTrace:
    language: str = DEFAULTS.speech.language
    journal: Journal | None = None
    request_id: str = ""
    transcript_id: str | None = None
    on_tool_start: Callable[[], None] = _ignore_tool_start
    on_intent: IntentHandler | None = None
    on_language: Callable[[str], None] | None = None
    speech_gate: SpeechGate | None = None
    review: ClaimReview | None = None


REPLY_STREAM_ATTEMPTS = 2
DEFAULT_STREAM_TRACE = StreamTrace()


def _correlated(config: RunnableConfig, trace: StreamTrace) -> RunnableConfig:
    keys = {"transcript_id": trace.transcript_id, "request_id": trace.request_id}
    metadata = {
        **config.get("metadata", {}),
        **{key: value for key, value in keys.items() if value},
    }
    return {**config, "metadata": metadata}


def _with_callback(config: RunnableConfig, callback: AsyncCallbackHandler) -> RunnableConfig:
    callbacks = config.get("callbacks")
    if isinstance(callbacks, BaseCallbackManager):
        manager = callbacks.copy()
        manager.add_handler(callback)
        return {**config, "callbacks": manager}
    return {**config, "callbacks": [*(callbacks or ()), callback]}


async def invoke_reply(
    agent: MeetingAgent,
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    trace: StreamTrace = DEFAULT_STREAM_TRACE,
) -> str:
    activity = _ToolActivity(trace.on_tool_start, trace.journal, transcript_id=trace.transcript_id)
    try:
        result = await agent.ainvoke(
            {"messages": list(messages)},
            config=_with_callback(_correlated(config, trace), activity),
        )
    except Exception as error:
        fault = classify_exception("agent", error)
        raise fault from error
    output = result.get("messages", [])
    if not output or not isinstance(output[-1], AIMessage) or output[-1].tool_calls:
        return ""
    return output[-1].text.strip()


def _hold_reviews(planner: ReplyPlanner) -> None:
    for index, header in enumerate(planner.claims):
        if header.act in OWNER_VIEWS:
            planner.hold_for_review(index)


def _plan_claims(planner: ReplyPlanner) -> list[CriticClaim]:
    return [
        CriticClaim(header, planner.claim_say(index)) for index, header in enumerate(planner.claims)
    ]


async def _accepted_claims(
    review: ClaimReview, claims: Sequence[CriticClaim]
) -> tuple[list[CriticClaim], set[int], list[CriticVerdict], str | None]:
    accepted: list[CriticClaim] = []
    indexes: set[int] = set()
    verdicts: list[CriticVerdict] = []
    first_reason: str | None = None
    for index, claim in enumerate(claims):
        outcome = await review.judge_plan([claim])
        if outcome.reason is None:
            accepted.append(claim)
            indexes.add(index)
            if outcome.verdict is not None:
                verdicts.append(outcome.verdict)
        elif first_reason is None:
            first_reason = outcome.reason
    return accepted, indexes, verdicts, first_reason


async def _close_segment(
    state: _SentenceStream, planner: ReplyPlanner, review: ClaimReview | None
) -> list[str]:
    spoken: list[str] = []
    tail = state.flush_segment()
    if tail is not None:
        spoken.extend(planner.take_sentence(tail, state.claim))
    if not state.buffer.strip():
        spoken.extend(await _settle_claim(state.claim, planner, review))
    return spoken


async def _settle_claim(claim: int, planner: ReplyPlanner, review: ClaimReview | None) -> list[str]:
    if review is None or claim not in planner.review_claims():
        return planner.end_claim(claim)
    header = planner.claim_header(claim)
    if header is None:
        return planner.end_claim(claim)
    accepted = await review.judge(header, planner.claim_say(claim))
    return planner.settle_claim(claim, accepted=accepted)


async def _planned_pieces(
    state: _SentenceStream,
    planner: ReplyPlanner,
    pieces: Sequence[SpeechPiece],
    review: ClaimReview | None,
) -> list[SpokenChunk] | None:
    if planner.silent:
        return None
    state.max_sentences = planner.max_sentences
    state.language = planner.language
    spoken: list[str] = []
    _hold_reviews(planner)
    for claim, content in pieces:
        if claim != state.claim:
            spoken.extend(await _close_segment(state, planner, review))
            state.claim = claim
        if not content:
            continue
        state.note_text(content)
        for sentence in state.take(content):
            spoken.extend(planner.take_sentence(sentence, claim))
    return [SpokenChunk(text=sentence, language=planner.language) for sentence in spoken]


async def _pump_stream(
    stream: AsyncIterator[Any],
    state: _SentenceStream,
    phase: _StreamPhase,
    planner: ReplyPlanner,
    review: ClaimReview | None,
) -> AsyncIterator[SpokenChunk]:
    snapshot = phase.boundary
    async for message, metadata in stream:
        piece, snapshot, consumable = _filter_piece(message, metadata, state, phase, snapshot)
        if not consumable:
            continue
        sentences = await _planned_pieces(state, planner, planner.feed(piece), review)
        if sentences is None:
            break
        for sentence in sentences:
            yield sentence
        if state.check_cap():
            break
    else:
        for sentence in await _planned_pieces(state, planner, planner.finish(), review) or ():
            yield sentence
        for line in await _close_segment(state, planner, review):
            yield SpokenChunk(text=line, language=planner.language)
    for line in planner.finish_sentences():
        yield SpokenChunk(text=line, language=planner.language)


def _record_reply_plan(
    journal: Journal | None, request_id: str, transcript_id: str | None, plan: PlanRead
) -> None:
    if journal is None:
        return
    journal.record(
        "reply_plan_observed",
        source="llm",
        request_id=request_id,
        transcript_id=transcript_id,
        structured=plan.structured,
        intent=plan.intent,
        language=plan.language,
        replaced=plan.replaced,
        proactive=plan.proactive,
        claims=[claim.model_dump(mode="json") for claim in plan.claims],
    )


def _record_speech_rejected(
    journal: Journal | None,
    request_id: str,
    transcript_id: str | None,
    verdict: SpeechVerdict,
    claim: ClaimHeader | None,
) -> None:
    if journal is None:
        return
    journal.record(
        "speech_rejected",
        source="agent",
        request_id=request_id,
        transcript_id=transcript_id,
        reason=verdict,
        claim=claim.model_dump(mode="json") if claim is not None else None,
    )


def _record_reply_text(state: _SentenceStream, reply: str) -> None:
    if not state.first_text and reply.strip():
        state.first_text = True
        if state.journal is not None:
            state.journal.record(
                "answer_text_emitted",
                source="llm",
                request_id=state.request_id,
                transcript_id=state.transcript_id,
            )


def _record_reply_cap(state: _SentenceStream, reply: str, capped_text: str) -> None:
    if capped_text != reply.strip() and state.journal is not None:
        state.journal.record(
            "reply_capped",
            source="agent",
            request_id=state.request_id,
            chars_in=len(reply),
            chars_out=len(capped_text),
        )


def _record_stream_failure(state: _SentenceStream, started: float) -> None:
    if state.journal is not None:
        state.journal.record(
            "agent_failed",
            source="agent",
            request_id=state.request_id,
            duration_ms=(monotonic() - started) * 1000,
            detail="stream",
        )


def _handlers(trace: StreamTrace) -> ReplyHandlers:
    return ReplyHandlers(
        on_intent=trace.on_intent,
        on_plan=partial(_record_reply_plan, trace.journal, trace.request_id, trace.transcript_id),
        on_language=trace.on_language,
        on_gate=partial(
            _record_speech_rejected, trace.journal, trace.request_id, trace.transcript_id
        ),
        on_sentence=lambda index, sentence, allowed: _record_validated_sentence(
            trace, index, sentence, authorized=allowed
        ),
    )


def _record_validated_sentence(
    trace: StreamTrace, index: int, sentence: str, *, authorized: bool
) -> None:
    if trace.journal is not None:
        trace.journal.record(
            "answer_sentence_accepted",
            source="agent",
            request_id=trace.request_id,
            transcript_id=trace.transcript_id,
            sentence_index=index,
            sentence_sha256=hashlib.sha256(" ".join(sentence.split()).encode()).hexdigest(),
            authorized_brief_question=authorized,
        )


def _reply_planner(max_sentences: int, trace: StreamTrace) -> ReplyPlanner:
    return ReplyPlanner(
        max_sentences=max_sentences,
        language=trace.language,
        handlers=_handlers(trace),
        speech_gate=trace.speech_gate,
    )


def _record_stream_retry(state: _SentenceStream, fault: ProviderFault, attempt: int) -> None:
    if state.journal is None:
        return
    state.journal.record(
        "reply_retried",
        source="agent",
        request_id=state.request_id,
        transcript_id=state.transcript_id,
        attempt=attempt,
        detail=fault.kind,
    )


async def stream_sentences(
    agent: MeetingAgent,
    messages: Sequence[BaseMessage],
    config: RunnableConfig,
    *,
    max_sentences: int,
    trace: StreamTrace = DEFAULT_STREAM_TRACE,
) -> AsyncIterator[SpokenChunk]:
    if max_sentences <= 0:
        raise ValueError("max_sentences must be positive")
    started = monotonic()
    phase = _StreamPhase(trace.on_tool_start, trace.journal, transcript_id=trace.transcript_id)
    state = _SentenceStream(
        trace.request_id, trace.transcript_id, max_sentences, trace.language, trace.journal
    )
    mark = SpeechMark()
    marked = SPEECH_MARK.set(mark)
    try:
        for attempt in range(REPLY_STREAM_ATTEMPTS):
            planner = _reply_planner(max_sentences, trace)
            stream: AsyncIterator[Any] = agent.astream(
                {"messages": list(messages)},
                config=_with_callback(_correlated(config, trace), phase),
                stream_mode="messages",
            )
            try:
                async for sentence in _pump_stream(stream, state, phase, planner, trace.review):
                    mark.started = True
                    yield sentence
            except (TimeoutError, FakeYouError):
                raise
            except Exception as error:
                fault = classify_exception("agent", error)
                if attempt + 1 < REPLY_STREAM_ATTEMPTS and not mark.started and fault.retryable:
                    _record_stream_retry(state, fault, attempt + 1)
                    continue
                _record_stream_failure(state, started)
                raise fault from error
            finally:
                await _close_stream_optional(stream, trace.journal)
            break
    finally:
        SPEECH_MARK.reset(marked)


class RecallFeed(Protocol):
    def observe_final(self, transcript_id: str, window: Sequence[str]) -> None: ...

    async def idle(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MeetingResponder:
    agent: MeetingAgent
    history: MeetingHistory
    journal: Journal | None = None
    language: str = DEFAULTS.speech.language
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences
    history_max_tokens: int = DEFAULTS.conversation.history_max_tokens
    control: ReplyControl | None = None
    turn_language: TurnLanguage = field(default_factory=TurnLanguage)
    contributor: MeetingAgent | None = None
    book_source: BookSource = field(default_factory=BookSource)
    evidence: TurnEvidence = field(default_factory=TurnEvidence)
    critic: ContributionCritic | None = None
    critic_threshold: float = 0.7
    recall_feed: RecallFeed | None = None

    def __post_init__(self) -> None:
        if self.max_sentences <= 0:
            raise ValueError("max_sentences must be positive")
        self.turn_language.language = self.language

    @property
    def limit(self) -> int:
        if self.control is None:
            return self.max_sentences
        return self.control.limit(self.max_sentences)

    def note_final(self, event: TranscriptEvent) -> None:
        if self.recall_feed is None or not event.finalized:
            return
        window = (*recent_room_lines(self.history.entries(), limit=7), event.text)
        self.recall_feed.observe_final(event.id, window)

    async def recall_idle(self) -> None:
        if self.recall_feed is not None:
            await self.recall_feed.idle()

    def messages(self, events: Sequence[TranscriptEvent]) -> list[BaseMessage]:
        earlier = history_messages(
            self.history.entries(),
            exclude={event.id for event in events},
            max_tokens=self.history_max_tokens,
        )
        return [*earlier, *request_messages(events)]

    def on_intent(self, intent: ReplyIntent) -> str | None:
        control = self.control
        if control is None:
            return None
        if intent == "shorter":
            control.sentence_cap = 1
            return None
        if intent == "louder":
            control.gain_steps += 1
            if control.on_louder is not None:
                control.on_louder()
            return None
        if intent == "repeat":
            return last_twin_text(self.history.entries())
        return None

    def _is_attention(self, events: Sequence[TranscriptEvent]) -> bool:
        if not events:
            return False
        last = events[-1]
        return last.attention is not None or last.source == "meeting_attention"

    def _agent_for(self, events: Sequence[TranscriptEvent]) -> MeetingAgent:
        if self.contributor is not None and self._is_attention(events):
            return self.contributor
        return self.agent

    def _book(self, events: Sequence[TranscriptEvent]) -> FactBook:
        transcript_id = events[-1].id if events else ""
        heard = "\n".join(
            (
                *recent_room_lines(self.history.entries(), limit=12),
                *(event.text for event in events),
            )
        )
        return fact_book(
            self.book_source,
            heard=heard,
            observed=self.evidence.observed(transcript_id),
            memory=self.evidence.memory(),
            screens=self.evidence.screens(),
        )

    def _trace(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        request_id: str = "",
        effects: HeldEffects | None = None,
    ) -> StreamTrace:
        on_intent: IntentHandler = self.on_intent
        on_language: Callable[[str], None] = self.turn_language.declare
        if effects is not None:
            effects.bind(on_intent, on_language)
            on_intent, on_language = effects.intent, effects.language
        gate = SpeechGate(
            partial(self._book, tuple(events)),
            proactive=self._is_attention(events),
            verify=self.book_source.verify,
        )
        return StreamTrace(
            language=self.language,
            journal=self.journal,
            request_id=request_id,
            transcript_id=events[-1].id if events else None,
            on_tool_start=on_tool_start,
            on_intent=on_intent,
            on_language=on_language,
            speech_gate=gate,
            review=self._review(gate, events, request_id),
        )

    def _review(
        self, gate: SpeechGate, events: Sequence[TranscriptEvent], request_id: str
    ) -> ClaimReview | None:
        if self.critic is None:
            return None
        last = events[-1] if events else None
        trigger = ""
        if last is not None:
            trigger = last.attention.trigger_text if last.attention else last.text
        return ClaimReview(
            critic=self.critic,
            gate=gate,
            threshold=self.critic_threshold,
            proactive=self._is_attention(events),
            trigger_text=trigger,
            recent_lines=tuple(recent_room_lines(self.history.entries())),
            journal=self.journal,
            request_id=request_id,
            transcript_id=events[-1].id if events else None,
        )

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        trace = self._trace(events, on_tool_start)
        reply = await invoke_reply(
            self._agent_for(events),
            self.messages(events),
            REPLY_CONFIG,
            trace=trace,
        )
        if not self._is_attention(events):
            return await self._respond_direct(reply, trace)
        return await self._respond_gated(reply, trace)

    async def _respond_direct(self, reply: str, trace: StreamTrace) -> SpokenChunk:
        planner = _reply_planner(self.limit, trace)
        pieces = [*planner.feed(reply), *planner.finish()]
        review = trace.review
        if review is not None:
            _hold_reviews(planner)
            claims = [claim for claim in _plan_claims(planner) if claim.header.act in OWNER_VIEWS]
            if claims:
                outcome = await review.judge_plan(claims)
                if outcome.reason is not None:
                    planner.refuse(planner.review_claims()[0])
                    return SpokenChunk(
                        text=" ".join(planner.finish_sentences()), language=planner.language
                    )
                for index in planner.review_claims():
                    planner.settle_claim(index, accepted=True)
        spoken = planner.speak_pieces(pieces)
        text = " ".join(spoken)
        if text:
            outputs = dict.fromkeys((text, cap_spoken_reply(text, self.limit, planner.language)))
            for output in outputs:
                _record_validated_sentence(
                    trace, 0, output, authorized=planner.authorized_reply_question(output)
                )
        return SpokenChunk(text=text, language=planner.language)

    async def _respond_gated(self, reply: str, trace: StreamTrace) -> SpokenChunk:
        planner = ReplyPlanner(
            max_sentences=1,
            language=self.language,
            handlers=_handlers(trace),
            speech_gate=trace.speech_gate,
        )
        pieces = [*planner.feed(reply), *planner.finish()]
        empty = SpokenChunk(text="", language=planner.language)
        if planner.silent:
            return empty
        claims = _plan_claims(planner)
        if not claims:
            planner.speak_pieces(pieces)
            return empty
        review = trace.review
        if review is None:
            return self._dropped(empty, trace, "critic_unavailable")
        accepted, accepted_indexes, verdicts, first_reason = await _accepted_claims(review, claims)
        if not accepted:
            return self._dropped(empty, trace, first_reason or "critic_failed")
        chunk = SpokenChunk(
            text=" ".join(
                planner.speak_pieces([piece for piece in pieces if piece.claim in accepted_indexes])
            ),
            language=planner.language,
        )
        if not chunk.text or planner.blocked is not None:
            return empty
        if self.journal is not None:
            self.journal.record(
                "contribution_verified",
                source="agent",
                request_id=trace.request_id,
                transcript_id=trace.transcript_id,
                claims=[claim.header.model_dump(mode="json") for claim in accepted],
                confidence=min((verdict.confidence for verdict in verdicts), default=None),
            )
        return chunk

    def _dropped(self, empty: SpokenChunk, trace: StreamTrace, reason: str) -> SpokenChunk:
        if self.journal is not None:
            self.journal.record(
                "contribution_dropped",
                source="agent",
                request_id=trace.request_id,
                transcript_id=trace.transcript_id,
                reason=reason,
            )
        return empty

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
        effects: HeldEffects | None = None,
    ) -> AsyncIterator[SpokenChunk]:
        if self._is_attention(events):
            return self._stream_gated(events, on_tool_start)
        return stream_sentences(
            self._agent_for(events),
            self.messages(events),
            REPLY_CONFIG,
            max_sentences=self.limit,
            trace=self._trace(events, on_tool_start, request_id, effects),
        )

    async def _stream_gated(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> AsyncIterator[SpokenChunk]:
        chunk = await self.respond(events, on_tool_start)
        if chunk.text:
            yield chunk


def cap_spoken_reply(
    reply: str,
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences,
    language: str = DEFAULTS.speech.language,
) -> str:
    if max_sentences <= 0:
        raise ValueError("max_sentences must be positive")
    if not reply.strip():
        return reply
    return " ".join(split_sentences(reply, language=language)[:max_sentences])
