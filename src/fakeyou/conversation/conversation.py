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
from fakeyou.conversation.control import ReplyControl
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
from fakeyou.observability.faults import ConsumerSink, guard_consumer
from fakeyou.observability.journal import Journal, JournalTiming
from fakeyou.observability.provider_faults import classify_exception


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
    planner = ReplyPlanner(
        max_sentences=max_sentences,
        language=trace.language,
        on_intent=trace.on_intent,
        on_plan=(
            None
            if trace.journal is None
            else partial(_record_reply_plan, trace.journal, trace.request_id, trace.transcript_id)
        ),
        on_language=trace.on_language,
    )
    mark = SpeechMark()
    marked = SPEECH_MARK.set(mark)
    try:
        stream: AsyncIterator[Any] = agent.astream(
            {"messages": list(messages)},
            config=_with_callback(config, phase),
            stream_mode="messages",
        )
        try:
            async for sentence in _pump_stream(stream, state, phase, planner):
                mark.started = True
                yield sentence
        finally:
            await _close_stream_optional(stream, trace.journal)
    except (TimeoutError, FakeYouError):
        raise
    except Exception as error:
        _record_stream_failure(state, started)
        raise classify_exception("agent", error) from error
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

    def _trace(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        request_id: str = "",
    ) -> StreamTrace:
        return StreamTrace(
            language=self.language,
            journal=self.journal,
            request_id=request_id,
            transcript_id=events[-1].id if events else None,
            on_tool_start=on_tool_start,
            on_tool_result=self.on_tool_result,
            on_intent=self.on_intent,
            on_language=self.turn_language.declare,
        )

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        reply = await invoke_reply(
            self.agent,
            self.messages(events),
            REPLY_CONFIG,
            trace=self._trace(events, on_tool_start),
        )
        return planned_reply(
            reply,
            max_sentences=self.limit,
            language=self.language,
            on_intent=self.on_intent,
        )

    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[SpokenChunk]:
        return stream_sentences(
            self.agent,
            self.messages(events),
            REPLY_CONFIG,
            max_sentences=self.limit,
            trace=self._trace(events, on_tool_start, request_id),
        )


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
