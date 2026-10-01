import asyncio
import hashlib
import json
import math
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol
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
from fakeyou.session import ConversationError as ConversationError
from fakeyou.session import MeetingAudio
from fakeyou.settings import DEFAULTS

ConversationPhase = Literal[
    "listening", "weighing", "queued", "interrupted", "waiting", "acting", "failed", "closed"
]


MAX_SPOKEN_SENTENCES = DEFAULTS.conversation.max_spoken_sentences
BARGE_IN_MIN_WORDS = DEFAULTS.conversation.barge_in_min_words


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


ToolResultCallback = Callable[[str, str, float], None]


class _ToolActivity(AsyncCallbackHandler):
    raise_error = True

    def __init__(
        self,
        on_start: Callable[[], None],
        journal: Journal | None,
        on_result: ToolResultCallback | None = None,
    ) -> None:
        self._on_start = on_start
        self._journal = journal
        self._on_result = on_result
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
            self._journal.request_started(str(run_id), source="llm", operation="chat")

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_finished(str(run_id), source="llm")

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        if self._journal is not None:
            self._journal.request_failed(str(run_id), source="llm", error=type(error).__name__)

    async def on_tool_start(
        self, serialized: dict[str, Any] | None, input_str: str, *, run_id: UUID, **kwargs: Any
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "tool"
        journal = self._journal or Journal("", stdout=False)
        context = journal.timed(
            finished="tool_finished", failed="tool_failed", source="agent", tool=str(name)
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


@dataclass(frozen=True, slots=True)
class LangGraphResponder:
    agent: Runnable[dict[str, list[HumanMessage]], dict[str, Any]]
    thread_id: str
    journal: Journal | None = None
    on_tool_result: ToolResultCallback | None = None

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str:
        messages = [
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
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config={
                    "configurable": {"thread_id": self.thread_id},
                    "recursion_limit": 30,
                    "callbacks": [_ToolActivity(on_tool_start, self.journal, self.on_tool_result)],
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


def cap_spoken_reply(
    reply: str, max_sentences: int = MAX_SPOKEN_SENTENCES, language: str = DEFAULTS.speech.language
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
        barge_in_min_words: int = BARGE_IN_MIN_WORDS,
        max_spoken_sentences: int = MAX_SPOKEN_SENTENCES,
        language: str = DEFAULTS.speech.language,
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
