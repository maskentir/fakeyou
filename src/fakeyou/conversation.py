import asyncio
import hashlib
import json
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable
from pipecat.frames.frames import TranscriptionFrame
from pipecat.utils.string import match_endofsentence

from fakeyou.faults import FaultReporter
from fakeyou.journal import Journal, JournalTiming
from fakeyou.provider_faults import classify_exception
from fakeyou.session import ConversationError, MeetingAudio
from fakeyou.settings import DEFAULTS

ConversationPhase = Literal[
    "listening", "weighing", "queued", "interrupted", "waiting", "acting", "failed", "closed"
]


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    id: str
    timestamp: str
    source: str
    text: str
    finalized: bool
    participant: str = "unknown"


def transcript_event(frame: TranscriptionFrame) -> TranscriptEvent:
    source = "meeting_audio"
    timestamp = frame.timestamp or datetime.now(UTC).isoformat()
    identity = json.dumps([source, frame.timestamp or frame.id, frame.text], ensure_ascii=False)
    return TranscriptEvent(
        id=hashlib.sha256(identity.encode()).hexdigest(),
        timestamp=timestamp,
        source=source,
        text=frame.text,
        finalized=bool(frame.finalized),
    )


def conversation_thread_id(owner_id: str, twin_id: str, meeting_id: str) -> str:
    identity = json.dumps([owner_id, twin_id, meeting_id])
    return f"meeting:{uuid5(NAMESPACE_URL, identity)}"


class ConversationResponder(Protocol):
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str: ...


@runtime_checkable
class StreamingConversationResponder(Protocol):
    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]: ...


ToolResultCallback = Callable[[str, str, float], None]


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

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_finished(
                str(run_id), source="llm", transcript_id=self._transcript_id
            )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_failed(
                str(run_id),
                source="llm",
                transcript_id=self._transcript_id,
                error=type(error).__name__,
            )

    async def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        journal = self._journal or Journal("", stdout=False)
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

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        text = output.text if isinstance(output, BaseMessage) else str(output)
        name, context, timing = self._running.pop(run_id)
        timing.fields["output_lines"] = len(text.splitlines())
        context.__exit__(None, None, None)
        if self._on_result is not None:
            self._on_result(name, text, timing.fields["duration_ms"])

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

    async def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        await super().on_tool_start(serialized, input_str, run_id=run_id, **kwargs)
        self.pending += 1
        self.boundary += 1
        self._counted.add(run_id)

    async def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            await super().on_tool_end(output, run_id=run_id, **kwargs)
        finally:
            if run_id in self._counted:
                self._counted.discard(run_id)
                self.pending -= 1
            self.boundary += 1

    async def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        try:
            await super().on_tool_error(error, run_id=run_id, **kwargs)
        finally:
            if run_id in self._counted:
                self._counted.discard(run_id)
                self.pending -= 1
            self.boundary += 1


def _request_messages(events: Sequence[TranscriptEvent]) -> list[HumanMessage]:
    return [
        HumanMessage(
            content=event.text,
            id=event.id,
            additional_kwargs={
                "meeting_event": {
                    "timestamp": event.timestamp,
                    "source": event.source,
                    "participant": event.participant,
                }
            },
        )
        for event in events
    ]


@dataclass(frozen=True, slots=True)
class LangGraphResponder:
    agent: Runnable[dict[str, list[HumanMessage]], dict[str, Any]]
    thread_id: str
    journal: Journal | None = None
    on_tool_result: ToolResultCallback | None = None

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str:
        messages = _request_messages(events)
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config={
                    "configurable": {"thread_id": self.thread_id},
                    "recursion_limit": 30,
                    "callbacks": [
                        _ToolActivity(
                            on_tool_start,
                            self.journal,
                            self.on_tool_result,
                            transcript_id=events[-1].id if events else None,
                        )
                    ],
                },
            )
        except Exception as error:
            fault = classify_exception("agent", error)
            raise fault from error
        output = result.get("messages", [])
        if not output or not isinstance(output[-1], AIMessage) or output[-1].tool_calls:
            reply = ""
        else:
            reply = output[-1].text.strip()
        return reply


@dataclass(frozen=True, slots=True)
class StreamingLangGraphResponder(LangGraphResponder):
    language: str = DEFAULTS.speech.language
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences

    def __post_init__(self) -> None:
        if self.max_sentences <= 0:
            raise ValueError("max_sentences must be positive")

    async def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        started = time.monotonic()
        transcript_id = events[-1].id if events else None
        phase = _StreamPhase(
            on_tool_start, self.journal, self.on_tool_result, transcript_id=transcript_id
        )
        buffer = ""
        yielded = 0
        spoken_chars = 0
        observed_chars = 0
        first_text = False
        capped = False
        snapshot = phase.boundary
        try:
            stream: AsyncIterator[Any] = self.agent.astream(
                {"messages": _request_messages(events)},
                config={
                    "configurable": {"thread_id": self.thread_id},
                    "recursion_limit": 30,
                    "callbacks": [phase],
                },
                stream_mode="messages",
            )
            try:
                async for message, metadata in stream:
                    if not isinstance(metadata, dict) or metadata.get("langgraph_node") != "model":
                        continue
                    if phase.boundary != snapshot:
                        snapshot = phase.boundary
                        buffer = ""
                    if phase.pending:
                        continue
                    piece = message.text if isinstance(message, BaseMessage) else ""
                    if not piece:
                        continue
                    observed_chars += len(piece)
                    if not first_text and (buffer + piece).strip():
                        first_text = True
                        if self.journal is not None:
                            self.journal.record(
                                "answer_text_emitted",
                                source="llm",
                                request_id=request_id,
                                transcript_id=transcript_id,
                            )
                    buffer += piece
                    while yielded < self.max_sentences:
                        boundary = match_endofsentence(buffer, language=self.language)
                        if not boundary:
                            break
                        sentence, buffer = buffer[:boundary].strip(), buffer[boundary:].lstrip()
                        if not sentence:
                            continue
                        if self.journal is not None:
                            self.journal.record(
                                "answer_sentence_emitted",
                                source="agent",
                                request_id=request_id,
                                transcript_id=transcript_id,
                                sentence_index=yielded,
                                chars=len(sentence),
                            )
                        yielded += 1
                        spoken_chars += len(sentence) + (1 if spoken_chars else 0)
                        yield sentence
                    if yielded >= self.max_sentences and buffer.strip():
                        if self.journal is not None:
                            self.journal.record(
                                "reply_capped",
                                source="agent",
                                request_id=request_id,
                                chars_in=observed_chars,
                                chars_out=spoken_chars,
                            )
                        capped = True
                        break
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    with suppress(Exception):
                        await close()
        except TimeoutError:
            raise
        except Exception as error:
            if yielded:
                raise classify_exception("agent", error) from error
            if self.journal is not None:
                self.journal.record(
                    "agent_failed",
                    source="agent",
                    request_id=request_id,
                    duration_ms=(time.monotonic() - started) * 1000,
                    detail="stream",
                )
            reply = await self.respond(events, on_tool_start)
            if not first_text and reply.strip():
                first_text = True
                if self.journal is not None:
                    self.journal.record(
                        "answer_text_emitted",
                        source="llm",
                        request_id=request_id,
                        transcript_id=transcript_id,
                    )
            capped_text = cap_spoken_reply(reply, self.max_sentences, self.language)
            if capped_text != reply.strip():
                if self.journal is not None:
                    self.journal.record(
                        "reply_capped",
                        source="agent",
                        request_id=request_id,
                        chars_in=len(reply),
                        chars_out=len(capped_text),
                    )
            rest = capped_text.strip()
            while rest:
                boundary = match_endofsentence(rest, language=self.language)
                if not boundary:
                    sentence = rest
                    rest = ""
                else:
                    sentence, rest = rest[:boundary].strip(), rest[boundary:].strip()
                    if not sentence:
                        continue
                if self.journal is not None:
                    self.journal.record(
                        "answer_sentence_emitted",
                        source="agent",
                        request_id=request_id,
                        transcript_id=transcript_id,
                        sentence_index=yielded,
                        chars=len(sentence),
                    )
                yielded += 1
                yield sentence
            return
        if not capped and yielded < self.max_sentences and buffer.strip():
            sentence = buffer.strip()
            if self.journal is not None:
                self.journal.record(
                    "answer_sentence_emitted",
                    source="agent",
                    request_id=request_id,
                    transcript_id=transcript_id,
                    sentence_index=yielded,
                    chars=len(sentence),
                )
            yield sentence


def cap_spoken_reply(
    reply: str,
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences,
    language: str = DEFAULTS.speech.language,
) -> str:
    if max_sentences <= 0:
        raise ValueError("max_sentences must be positive")
    if not reply.strip():
        return reply
    remaining = reply.strip()
    sentences: list[str] = []
    for _ in range(max_sentences):
        boundary = match_endofsentence(remaining, language=language)
        if not boundary:
            sentences.append(remaining)
            break
        sentences.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
        if not remaining:
            break
    return " ".join(sentences)


class ConversationLoop:
    def __init__(
        self,
        responder: ConversationResponder,
        *,
        on_transition: Callable[[ConversationPhase, ConversationPhase, str], None] | None = None,
        response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds,
        wake_phrases: Sequence[str] = (),
        activation_seconds: float = DEFAULTS.conversation.activation_seconds,
        barge_in: bool = False,
        barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words,
        max_spoken_sentences: int = DEFAULTS.conversation.max_spoken_sentences,
        language: str = DEFAULTS.speech.language,
        sentence_streaming: bool = DEFAULTS.conversation.sentence_streaming,
        clock: Callable[[], float] = time.monotonic,
        faults: FaultReporter | None = None,
        journal: Journal | None = None,
    ) -> None:
        if not math.isfinite(response_timeout_seconds) or response_timeout_seconds <= 0:
            raise ValueError("response timeout must be positive and finite")
        if not math.isfinite(activation_seconds) or activation_seconds <= 0:
            raise ValueError("activation window must be positive and finite")
        self._faults = faults
        self._journal = journal
        self._responder = responder
        self._on_transition = on_transition
        self._response_timeout = response_timeout_seconds
        self._wake_phrases = tuple(
            phrase.strip().casefold() for phrase in wake_phrases if phrase.strip()
        )
        self._activation_seconds = activation_seconds
        self._barge_in_min_words = barge_in_min_words
        self._max_spoken_sentences = max_spoken_sentences
        self._language = language
        self._sentence_streaming = sentence_streaming
        self._stream_stops = 0
        self._barge_in = barge_in
        self._clock = clock
        self._active_until = 0.0
        self._session: MeetingAudio | None = None
        self._phase: ConversationPhase = "listening"
        self._events: list[TranscriptEvent] = []
        self._pending: deque[TranscriptEvent] = deque()
        self._seen: set[str] = set()
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._failure: ConversationError | None = None
        self._closed = False
        self._interrupted = False

    @property
    def is_busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def phase(self) -> ConversationPhase:
        return self._phase

    @property
    def events(self) -> tuple[TranscriptEvent, ...]:
        return tuple(self._events)

    def bind(self, session: MeetingAudio) -> None:
        if self._closed:
            raise ConversationError("meeting conversation is closed")
        if self._session is not None and self._session is not session:
            raise ConversationError("meeting conversation already owns an audio session")
        self._session = session
        self._start_pending()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def handle_transcript(self, event: TranscriptEvent) -> None:
        if (
            self._closed
            or self._failure is not None
            or not event.finalized
            or not event.text.strip()
        ):
            return
        if event.id in self._seen:
            return
        self._seen.add(event.id)
        self._events.append(event)
        if self._wake_phrases and not self._addressed(event):
            return
        self._pending.append(event)
        self._generation += 1
        if self._task is not None and not self._task.done():
            self._move("interrupted", event.id)
        if self._session is not None and self._barge_in and self._session.is_speaking:
            self._record("barge_in_detected", trigger="final", words=len(event.text.split()))
        if self._session is not None and (
            self._barge_in
            and self._session.is_speaking
            or self.is_busy
            and not self._session.is_speaking
        ):
            await self._session.stop_playback()
        self._start_pending()

    async def handle_interim(self, text: str) -> None:
        session = self._session
        if (
            self._closed
            or self._failure is not None
            or not self._barge_in
            or session is None
            or not session.is_speaking
            or self._wake_phrases
            and self._clock() >= self._active_until
        ):
            return
        words = len(text.split())
        if words >= self._barge_in_min_words and not self._interrupted:
            self._interrupted = True
            self._record("barge_in_detected", trigger="interim", words=words)
            await session.stop_playback()
            self._stream_stops += 1

    def _addressed(self, event: TranscriptEvent) -> bool:
        now = self._clock()
        if any(phrase in event.text.casefold() for phrase in self._wake_phrases):
            self._active_until = now + self._activation_seconds
            return True
        return now < self._active_until

    async def wait_idle(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)
        self.raise_if_failed()

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._pending.clear()
        self._session = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._phase = "closed"

    def _start_pending(self) -> None:
        if self._closed or self._failure is not None or self._session is None or not self._pending:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._respond_pending())

    def _move(self, phase: ConversationPhase, detail: str) -> None:
        previous, self._phase = self._phase, phase
        if self._on_transition is not None:
            try:
                self._on_transition(previous, phase, detail)
            except Exception as error:
                if self._faults is not None:
                    self._faults.record(
                        classify_exception("agent", error, operation="transition_observer")
                    )

    def _record(self, event: str, **fields: Any) -> None:
        if self._journal is not None:
            self._journal.record(event, source="agent", **fields)

    def _fail(self, cause: Exception) -> None:
        if self._failure is None:
            if self._faults is not None:
                self._faults.record(classify_exception("agent", cause))
            self._failure = ConversationError("meeting conversation failed")
            self._failure.__cause__ = cause
            self._phase = "failed"

    def _tool_started(self, generation: int, event_id: str) -> None:
        if generation == self._generation and not self._closed:
            self._move("acting", event_id)
            self.raise_if_failed()

    async def _speak_streamed(
        self,
        responder: StreamingConversationResponder,
        events: tuple[TranscriptEvent, ...],
        generation: int,
        event_id: str,
        request_id: str,
        started: float,
    ) -> None:
        stop_mark = self._stream_stops
        spoken = 0
        try:
            async with asyncio.timeout(self._response_timeout):
                stream = responder.stream_sentences(
                    events, partial(self._tool_started, generation, event_id), request_id=request_id
                )
                try:
                    async for sentence in stream:
                        if (
                            self._closed
                            or generation != self._generation
                            or self._stream_stops != stop_mark
                        ):
                            if self._closed:
                                detail = "closed"
                            elif generation != self._generation:
                                detail = "stale_generation"
                            else:
                                detail = "stopped"
                            self._record(
                                "sentence_dropped",
                                request_id=request_id,
                                transcript_id=event_id,
                                sentence_index=spoken,
                                detail=detail,
                            )
                            self._record(
                                "agent_cancelled",
                                request_id=request_id,
                                duration_ms=(self._clock() - started) * 1000,
                            )
                            return
                        session = self._session
                        if session is None:
                            self._record(
                                "sentence_dropped",
                                request_id=request_id,
                                transcript_id=event_id,
                                sentence_index=spoken,
                                detail="closed",
                            )
                            self._record(
                                "agent_cancelled",
                                request_id=request_id,
                                duration_ms=(self._clock() - started) * 1000,
                            )
                            return
                        if not spoken:
                            self._move("queued", event_id)
                        self.raise_if_failed()
                        self._interrupted = False
                        await session.speak(sentence, sentence_index=spoken, transcript_id=event_id)
                        spoken += 1
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        with suppress(Exception):
                            await close()
        except BaseException as error:
            self._record(
                "agent_cancelled" if isinstance(error, asyncio.CancelledError) else "agent_failed",
                request_id=request_id,
                duration_ms=(self._clock() - started) * 1000,
            )
            raise
        else:
            self._record(
                "agent_finished",
                request_id=request_id,
                duration_ms=(self._clock() - started) * 1000,
            )
        self.raise_if_failed()
        if generation != self._generation or self._closed:
            return
        if not spoken:
            self._move("waiting", event_id)
        else:
            self._move("listening", event_id)

    async def _respond_pending(self) -> None:
        try:
            while self._pending and not self._closed:
                events = tuple(self._pending)
                self._pending.clear()
                generation = self._generation
                event_id = events[-1].id
                self._move("weighing", event_id)
                self.raise_if_failed()
                started = self._clock()
                request_id = f"{event_id}:{generation}"
                self._record("agent_started", request_id=request_id)
                responder = self._responder
                if self._sentence_streaming and isinstance(
                    responder, StreamingConversationResponder
                ):
                    await self._speak_streamed(
                        responder, events, generation, event_id, request_id, started
                    )
                    continue
                try:
                    async with asyncio.timeout(self._response_timeout):
                        reply = await self._responder.respond(
                            events, partial(self._tool_started, generation, event_id)
                        )
                except BaseException as error:
                    self._record(
                        "agent_cancelled"
                        if isinstance(error, asyncio.CancelledError)
                        else "agent_failed",
                        request_id=request_id,
                        duration_ms=(self._clock() - started) * 1000,
                    )
                    raise
                else:
                    self._record(
                        "agent_finished",
                        request_id=request_id,
                        duration_ms=(self._clock() - started) * 1000,
                    )
                self.raise_if_failed()
                if generation != self._generation or self._closed:
                    continue
                session = self._session
                if not reply.strip() or session is None:
                    self._move("waiting", event_id)
                    continue
                spoken = cap_spoken_reply(reply, self._max_spoken_sentences, self._language)
                if spoken != reply.strip():
                    self._record(
                        "reply_capped",
                        request_id=request_id,
                        chars_in=len(reply),
                        chars_out=len(spoken),
                    )
                self._move("queued", event_id)
                self.raise_if_failed()
                self._interrupted = False
                await session.speak(spoken)
                if generation == self._generation:
                    self._move("listening", event_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
