import time
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
from fakeyou.conversation.contribution import (
    ContributionGate,
    GateVerdict,
    SourceRegistry,
    memory_refs_from_evidence,
)
from fakeyou.conversation.control import ReplyControl
from fakeyou.conversation.grounding import GroundingVerifier
from fakeyou.conversation.meeting_messages import (
    MeetingHistory,
    history_messages,
    last_twin_text,
    request_messages,
)
from fakeyou.conversation.middleware import SPEECH_MARK, SpeechMark
from fakeyou.conversation.reply_planner import (
    IntentHandler,
    PlanRead,
    ReplyHandlers,
    ReplyIntent,
    ReplyPlanner,
    SpokenChunk,
    TurnLanguage,
    planned_reply,
)
from fakeyou.conversation.sentence_stream import SentenceStream
from fakeyou.foundation.protocols import SupportsAclose
from fakeyou.foundation.sentences import split_sentences
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import ConsumerSink, capture_error, guard_consumer
from fakeyou.observability.journal import Journal, JournalTiming
from fakeyou.observability.provider_faults import ProviderFault, classify_exception


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
    ) -> AsyncIterator[SpokenChunk]: ...


ToolResultCallback = Callable[[str, str, float], None]
MeetingAgent = Runnable[dict[str, list[BaseMessage]], dict[str, Any]]
REPLY_CONFIG: RunnableConfig = {"recursion_limit": 30}


class _ToolActivity(AsyncCallbackHandler):
    raise_error = True

    def __init__(
        self,
        on_start: Callable[[], None],
        journal: Journal | None,
        on_result: ToolResultCallback | None = None,
        *,
        transcript_id: str | None = None,
    ) -> None:
        self._on_start = on_start
        self._journal = journal
        self._on_result = on_result
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
        name, context, timing = self._running.pop(run_id)
        timing.fields["output_lines"] = len(text.splitlines())
        context.__exit__(None, None, None)
        if self._on_result is not None:
            self._on_result(name, text, timing.fields["duration_ms"])

    @override
    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        _, context, _ = self._running.pop(run_id)
        context.__exit__(type(error), error, error.__traceback__)


class _StreamPhase(_ToolActivity):
    def __init__(
        self,
        on_start: Callable[[], None],
        journal: Journal | None,
        on_result: ToolResultCallback | None = None,
        *,
        transcript_id: str | None = None,
    ) -> None:
        super().__init__(on_start, journal, on_result, transcript_id=transcript_id)
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

    def record_sentence(self, sentence: str, index: int | None = None) -> None:
        if self.journal is not None:
            self.journal.record(
                "answer_sentence_emitted",
                source="agent",
                request_id=self.request_id,
                transcript_id=self.transcript_id,
                sentence_index=self.yielded if index is None else index,
                chars=len(sentence),
            )

    @override
    def take(self, piece: str) -> list[str]:
        start = self.yielded
        sentences = super().take(piece)
        for offset, sentence in enumerate(sentences):
            self.record_sentence(sentence, start + offset)
            self.spoken_chars += len(sentence) + (1 if self.spoken_chars else 0)
        return sentences

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
class StreamTrace:
    language: str = DEFAULTS.speech.language
    journal: Journal | None = None
    request_id: str = ""
    transcript_id: str | None = None
    on_tool_start: Callable[[], None] = _ignore_tool_start
    on_tool_result: ToolResultCallback | None = None
    on_intent: IntentHandler | None = None
    on_language: Callable[[str], None] | None = None
    contribution_gate: ContributionGate | None = None


REPLY_STREAM_ATTEMPTS = 2
_RECALL_TOOL = "recall_meeting_context"
DEFAULT_STREAM_TRACE = StreamTrace()


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
    activity = _ToolActivity(
        trace.on_tool_start, trace.journal, trace.on_tool_result, transcript_id=trace.transcript_id
    )
    try:
        result = await agent.ainvoke(
            {"messages": list(messages)}, config=_with_callback(config, activity)
        )
    except Exception as error:
        fault = classify_exception("agent", error)
        raise fault from error
    output = result.get("messages", [])
    if not output or not isinstance(output[-1], AIMessage) or output[-1].tool_calls:
        return ""
    return output[-1].text.strip()


def _planned_piece(
    state: _SentenceStream, planner: ReplyPlanner, piece: str
) -> list[SpokenChunk] | None:
    content = planner.feed(piece)
    if planner.silent:
        return None
    if not content:
        return []
    state.note_text(content)
    state.max_sentences = planner.max_sentences
    state.language = planner.language
    spoken: list[str] = []
    for sentence in state.take(content):
        spoken.extend(planner.take_sentence(sentence))
    return [SpokenChunk(text=sentence, language=planner.language) for sentence in spoken]


async def _pump_stream(
    stream: AsyncIterator[Any],
    state: _SentenceStream,
    phase: _StreamPhase,
    planner: ReplyPlanner,
) -> AsyncIterator[SpokenChunk]:
    snapshot = phase.boundary
    async for message, metadata in stream:
        piece, snapshot, consumable = _filter_piece(message, metadata, state, phase, snapshot)
        if not consumable:
            continue
        sentences = _planned_piece(state, planner, piece)
        if sentences is None:
            return
        for sentence in sentences:
            yield sentence
        if state.check_cap():
            return
    tail = state.flush()
    if tail is not None:
        state.record_sentence(tail)
        for line in planner.take_sentence(tail):
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
        kind=plan.kind,
        plan_source=plan.source,
        source_ref=plan.source_ref,
        gate=plan.gate,
        gate_reason=plan.gate_reason,
    )
    if plan.gate == "drop":
        journal.record(
            "contribution_dropped",
            source="agent",
            request_id=request_id,
            transcript_id=transcript_id,
            reason=plan.gate_reason,
            kind=plan.kind,
            plan_source=plan.source,
            source_ref=plan.source_ref,
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
            duration_ms=(time.monotonic() - started) * 1000,
            detail="stream",
        )


def _reply_planner(max_sentences: int, trace: StreamTrace) -> ReplyPlanner:
    return ReplyPlanner(
        max_sentences=max_sentences,
        language=trace.language,
        handlers=ReplyHandlers(
            on_intent=trace.on_intent,
            on_plan=(
                None
                if trace.journal is None
                else partial(
                    _record_reply_plan, trace.journal, trace.request_id, trace.transcript_id
                )
            ),
            on_language=trace.on_language,
        ),
        contribution_gate=trace.contribution_gate,
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
    started = time.monotonic()
    phase = _StreamPhase(
        trace.on_tool_start, trace.journal, trace.on_tool_result, transcript_id=trace.transcript_id
    )
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
                config=_with_callback(config, phase),
                stream_mode="messages",
            )
            try:
                async for sentence in _pump_stream(stream, state, phase, planner):
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


@dataclass(frozen=True, slots=True)
class MeetingResponder:
    agent: MeetingAgent
    history: MeetingHistory
    journal: Journal | None = None
    on_tool_result: ToolResultCallback | None = None
    language: str = DEFAULTS.speech.language
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences
    history_max_tokens: int = DEFAULTS.conversation.history_max_tokens
    control: ReplyControl | None = None
    turn_language: TurnLanguage = field(default_factory=TurnLanguage)
    contributor: MeetingAgent | None = None
    source_registry: SourceRegistry = field(default_factory=SourceRegistry)
    verifier: GroundingVerifier | None = None
    grounding_threshold: float = 0.7

    def __post_init__(self) -> None:
        if self.max_sentences <= 0:
            raise ValueError("max_sentences must be positive")
        self.turn_language.language = self.language

    @property
    def limit(self) -> int:
        if self.control is None:
            return self.max_sentences
        return self.control.limit(self.max_sentences)

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

    def _gate_for(
        self, events: Sequence[TranscriptEvent], memory_refs: dict[str, str]
    ) -> ContributionGate | None:
        if not self._is_attention(events):
            return None
        last = events[-1]
        trigger = last.attention.trigger_text if last.attention is not None else last.text
        return ContributionGate(
            registry=self.source_registry,
            trigger_text=trigger,
            memory_refs=lambda: memory_refs,
        )

    def _trace(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        request_id: str = "",
    ) -> StreamTrace:
        memory_refs: dict[str, str] = {}
        downstream = self.on_tool_result

        def on_tool_result(name: str, text: str, duration_ms: float) -> None:
            if name == _RECALL_TOOL:
                memory_refs.update(memory_refs_from_evidence(text))
            if downstream is not None:
                downstream(name, text, duration_ms)

        return StreamTrace(
            language=self.language,
            journal=self.journal,
            request_id=request_id,
            transcript_id=events[-1].id if events else None,
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,
            on_intent=self.on_intent,
            on_language=self.turn_language.declare,
            contribution_gate=self._gate_for(events, memory_refs),
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
        if trace.contribution_gate is None:
            return planned_reply(
                reply,
                max_sentences=self.limit,
                language=self.language,
                on_intent=self.on_intent,
            )
        return await self._respond_gated(reply, trace)

    async def _respond_gated(self, reply: str, trace: StreamTrace) -> SpokenChunk:
        reads: list[PlanRead] = []
        planner = ReplyPlanner(
            max_sentences=1,
            language=self.language,
            handlers=ReplyHandlers(
                on_intent=self.on_intent,
                on_plan=reads.append,
                on_language=self.turn_language.declare,
            ),
            contribution_gate=trace.contribution_gate,
        )
        content = planner.feed(reply) + planner.finish()
        for read in reads:
            _record_reply_plan(self.journal, trace.request_id, trace.transcript_id, read)
        empty = SpokenChunk(text="", language=planner.language)
        if planner.silent or not content.strip():
            return empty
        spoken: list[str] = []
        for sentence in split_sentences(content, language=planner.language):
            spoken.extend(planner.take_sentence(sentence))
        spoken.extend(planner.finish_sentences())
        chunk = SpokenChunk(text=" ".join(spoken), language=planner.language)
        if not chunk.text:
            return empty
        if await self._verified(chunk, planner.verdict, trace):
            return chunk
        return empty

    async def _verified(
        self, chunk: SpokenChunk, verdict: GateVerdict | None, trace: StreamTrace
    ) -> bool:
        empty = SpokenChunk(text="", language=chunk.language)

        def drop(reason: str) -> bool:
            self._dropped(empty, trace, reason)
            return False

        gate = trace.contribution_gate
        if verdict is None or verdict.source_text is None or gate is None:
            return drop("missing_verdict")
        if self.verifier is None:
            return drop("verifier_unavailable")
        checked, error = await capture_error(
            partial(
                self.verifier.verify,
                text=chunk.text,
                source_text=verdict.source_text,
                trigger_text=gate.trigger_text,
                kind=verdict.kind,
            )
        )
        if checked is None:
            return drop("verifier_failed")
        if not checked.grounded:
            return drop("not_grounded")
        if not checked.adds_information:
            return drop("adds_nothing")
        if checked.confidence < self.grounding_threshold:
            return drop("low_confidence")
        if self.journal is not None:
            self.journal.record(
                "contribution_verified",
                source="agent",
                request_id=trace.request_id,
                transcript_id=trace.transcript_id,
                kind=verdict.kind,
                plan_source=verdict.source,
                source_ref=verdict.source_ref,
                confidence=checked.confidence,
            )
        return True

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
    ) -> AsyncIterator[SpokenChunk]:
        if self._is_attention(events):
            return self._stream_gated(events, on_tool_start)
        return stream_sentences(
            self._agent_for(events),
            self.messages(events),
            REPLY_CONFIG,
            max_sentences=self.limit,
            trace=self._trace(events, on_tool_start, request_id),
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
