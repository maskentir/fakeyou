import asyncio
import json
import math
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from importlib.resources import files
from typing import Any, Literal, Protocol, cast, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import LLMResult
from langchain_core.runnables import Runnable
from pipecat.utils.string import match_endofsentence

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.endpointing import SemanticEndpoint
from fakeyou.conversation.session import ConversationError, MeetingAudio
from fakeyou.foundation.participation import (
    ParticipationContext,
    choose_participation,
    is_vocative,
    same_observed_speaker,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.utterance_quality import describe_word_diff, normalize_words
from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.journal import Journal, JournalTiming
from fakeyou.observability.provider_faults import classify_exception

ConversationPhase = Literal[
    "listening", "weighing", "queued", "interrupted", "waiting", "acting", "failed", "closed"
]


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

_ACK_VOCABULARY: dict[str, Any] = json.loads(
    (files("fakeyou") / "ack_vocabulary.json").read_text(encoding="utf-8")
)
BARE_ACK_VOCABULARY: frozenset[str] = frozenset(_ACK_VOCABULARY["bare_acks"])
_ACK_EDGE_PUNCTUATION = '.,!;:"“”‘’()[]{}-–—…'


def _is_bare_ack(text: str) -> bool:
    if "?" in text:
        return False
    words = [word.strip(_ACK_EDGE_PUNCTUATION) for word in text.casefold().split()]
    words = [word for word in words if word]
    return len(words) == 1 and words[0] in BARE_ACK_VOCABULARY


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
    messages: list[HumanMessage] = []
    for event in events:
        meeting: dict[str, Any] = {
            "timestamp": event.timestamp,
            "source": event.source,
            "participant_id": event.participant_id,
            "speaker_evidence_id": event.speaker_evidence_id,
            "stream_id": event.stream_id,
            "speaker_status": "observed" if event.participant_id != "unknown" else "unknown",
        }
        if event.visual_refs:
            meeting["visual_refs"] = list(event.visual_refs)
        messages.append(
            HumanMessage(
                content=event.text,
                id=event.id,
                additional_kwargs={"meeting_event": meeting},
            )
        )
    return messages


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
        except asyncio.CancelledError:
            await self._reconcile_cancelled_tools()
            raise
        except Exception as error:
            fault = classify_exception("agent", error)
            raise fault from error
        output = result.get("messages", [])
        if not output or not isinstance(output[-1], AIMessage) or output[-1].tool_calls:
            reply = ""
        else:
            reply = output[-1].text.strip()
        return reply

    async def _reconcile_cancelled_tools(self) -> None:
        get_state = getattr(self.agent, "aget_state", None)
        update_state = getattr(self.agent, "aupdate_state", None)
        if not callable(get_state) or not callable(update_state):
            return
        config = {"configurable": {"thread_id": self.thread_id}}
        try:
            state = await get_state(config)
        except Exception:
            return
        messages = state.values.get("messages", [])
        answered = {
            message.tool_call_id for message in messages if isinstance(message, ToolMessage)
        }
        pending_calls = [
            call["id"]
            for message in messages
            if isinstance(message, AIMessage) and message.tool_calls
            for call in message.tool_calls
            if call["id"] not in answered
        ]
        if not pending_calls:
            return
        try:
            await update_state(
                config,
                {
                    "messages": [
                        ToolMessage(content="Tool execution was cancelled.", tool_call_id=call_id)
                        for call_id in pending_calls
                    ]
                },
                as_node="tools",
            )
        except Exception:
            return


@dataclass(slots=True)
class _SentenceStream:
    request_id: str
    transcript_id: str | None
    max_sentences: int
    language: str
    journal: Journal | None
    buffer: str = ""
    yielded: int = 0
    spoken_chars: int = 0
    observed_chars: int = 0
    first_text: bool = False
    capped: bool = False

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

    def record_sentence(self, sentence: str) -> None:
        if self.journal is not None:
            self.journal.record(
                "answer_sentence_emitted",
                source="agent",
                request_id=self.request_id,
                transcript_id=self.transcript_id,
                sentence_index=self.yielded,
                chars=len(sentence),
            )

    def drain(self) -> Iterator[str]:
        while self.yielded < self.max_sentences:
            boundary = match_endofsentence(self.buffer, language=self.language)
            if not boundary:
                break
            sentence, self.buffer = (
                self.buffer[:boundary].strip(),
                self.buffer[boundary:].lstrip(),
            )
            if not sentence:
                continue
            self.record_sentence(sentence)
            self.yielded += 1
            self.spoken_chars += len(sentence) + (1 if self.spoken_chars else 0)
            yield sentence

    def check_cap(self) -> bool:
        if self.yielded >= self.max_sentences and self.buffer.strip():
            if self.journal is not None:
                self.journal.record(
                    "reply_capped",
                    source="agent",
                    request_id=self.request_id,
                    chars_in=self.observed_chars,
                    chars_out=self.spoken_chars,
                )
            self.capped = True
            return True
        return False

    def flush(self) -> str | None:
        if not self.capped and self.yielded < self.max_sentences and self.buffer.strip():
            return self.buffer.strip()
        return None


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


async def _close_stream_optional(stream: AsyncIterator[Any]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        with suppress(Exception):
            await close()


@dataclass(frozen=True, slots=True)
class StreamingLangGraphResponder(LangGraphResponder):
    language: str = DEFAULTS.speech.language
    max_sentences: int = DEFAULTS.conversation.max_spoken_sentences

    def __post_init__(self) -> None:
        if self.max_sentences <= 0:
            raise ValueError("max_sentences must be positive")

    async def _pump_stream(
        self, stream: AsyncIterator[Any], state: _SentenceStream, phase: _StreamPhase
    ) -> AsyncIterator[str]:
        snapshot = phase.boundary
        async for message, metadata in stream:
            piece, snapshot, consumable = _filter_piece(message, metadata, state, phase, snapshot)
            if not consumable:
                continue
            state.note_text(piece)
            state.buffer += piece
            for sentence in state.drain():
                yield sentence
            if state.check_cap():
                return
        tail = state.flush()
        if tail is not None:
            state.record_sentence(tail)
            yield tail

    def _record_reply_text(self, state: _SentenceStream, reply: str) -> None:
        if not state.first_text and reply.strip():
            state.first_text = True
            if self.journal is not None:
                self.journal.record(
                    "answer_text_emitted",
                    source="llm",
                    request_id=state.request_id,
                    transcript_id=state.transcript_id,
                )

    def _record_reply_cap(self, state: _SentenceStream, reply: str, capped_text: str) -> None:
        if capped_text != reply.strip():
            if self.journal is not None:
                self.journal.record(
                    "reply_capped",
                    source="agent",
                    request_id=state.request_id,
                    chars_in=len(reply),
                    chars_out=len(capped_text),
                )

    async def _fallback_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        state: _SentenceStream,
    ) -> AsyncIterator[str]:
        reply = await self.respond(events, on_tool_start)
        self._record_reply_text(state, reply)
        capped_text = cap_spoken_reply(reply, self.max_sentences, self.language)
        self._record_reply_cap(state, reply, capped_text)
        state.buffer = capped_text.strip()
        for sentence in state.drain():
            yield sentence
        rest = state.buffer.strip()
        if rest:
            state.record_sentence(rest)
            state.yielded += 1
            yield rest

    def _record_stream_failure(self, state: _SentenceStream, started: float) -> None:
        if self.journal is not None:
            self.journal.record(
                "agent_failed",
                source="agent",
                request_id=state.request_id,
                duration_ms=(time.monotonic() - started) * 1000,
                detail="stream",
            )

    async def _recover_stream(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        state: _SentenceStream,
        started: float,
    ) -> AsyncIterator[str]:
        self._record_stream_failure(state, started)
        async for sentence in self._fallback_sentences(events, on_tool_start, state):
            yield sentence

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
        state = _SentenceStream(
            request_id, transcript_id, self.max_sentences, self.language, self.journal
        )
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
                async for sentence in self._pump_stream(stream, state, phase):
                    yield sentence
            finally:
                await _close_stream_optional(stream)
        except TimeoutError:
            raise
        except Exception as error:
            if state.yielded:
                raise classify_exception("agent", error) from error
            async for sentence in self._recover_stream(events, on_tool_start, state, started):
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


@dataclass(frozen=True, slots=True)
class StreamTurn:
    generation: int
    event_id: str
    request_id: str
    started: float


@dataclass(slots=True)
class _Pregeneration:
    words: tuple[str, ...]
    request_id: str
    started: float
    generation: int
    queue: asyncio.Queue[str | None]
    buffered: list[str]
    task: asyncio.Task[None] | None = None
    stream: AsyncIterator[str] | None = None
    adopted: bool = False


def _pregen_failure_reason(task: asyncio.Task[None] | None) -> str | None:
    if task is None or not task.done():
        return None
    if task.cancelled():
        return "error"
    error = task.exception()
    if error is None:
        return None
    if isinstance(error, TimeoutError):
        return "expired"
    return "error"


@dataclass(frozen=True, slots=True)
class LoopInteraction:
    on_transition: Callable[[ConversationPhase, ConversationPhase, str], None] | None = None
    wake_phrases: Sequence[str] = ()
    barge_in: bool = False
    barge_in_min_words: int = DEFAULTS.conversation.barge_in_min_words
    participant_policy_enabled: bool = False

    def __post_init__(self) -> None:
        phrases = tuple(phrase.strip().casefold() for phrase in self.wake_phrases if phrase.strip())
        object.__setattr__(self, "wake_phrases", phrases)
        if self.participant_policy_enabled and not phrases:
            raise ValueError("participant policy requires configured twin aliases")


DEFAULT_LOOP_INTERACTION = LoopInteraction()


@dataclass(frozen=True, slots=True)
class LoopTiming:
    response_timeout_seconds: float = DEFAULTS.conversation.response_timeout_seconds
    activation_seconds: float = DEFAULTS.conversation.activation_seconds
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if not math.isfinite(self.response_timeout_seconds) or self.response_timeout_seconds <= 0:
            raise ValueError("response timeout must be positive and finite")
        if not math.isfinite(self.activation_seconds) or self.activation_seconds <= 0:
            raise ValueError("activation window must be positive and finite")


DEFAULT_LOOP_TIMING = LoopTiming()


@dataclass(frozen=True, slots=True)
class LoopGeneration:
    max_spoken_sentences: int = DEFAULTS.conversation.max_spoken_sentences
    language: str = DEFAULTS.speech.language
    sentence_streaming: bool = DEFAULTS.conversation.sentence_streaming
    pre_generate_enabled: bool = DEFAULTS.conversation.pre_generate_enabled
    pregen_fuzzy_restart_enabled: bool = DEFAULTS.conversation.pregen_fuzzy_restart_enabled


DEFAULT_LOOP_GENERATION = LoopGeneration()


class ConversationLoop:
    def __init__(
        self,
        responder: ConversationResponder | StreamingConversationResponder,
        *,
        interaction: LoopInteraction = DEFAULT_LOOP_INTERACTION,
        timing: LoopTiming = DEFAULT_LOOP_TIMING,
        generation: LoopGeneration = DEFAULT_LOOP_GENERATION,
        diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
    ) -> None:
        self._faults = diagnostics.faults
        self._journal = diagnostics.journal
        self._responder = responder
        self._on_transition = interaction.on_transition
        self._response_timeout = timing.response_timeout_seconds
        self._wake_phrases = interaction.wake_phrases
        self._participant_policy_enabled = interaction.participant_policy_enabled
        self._last_speaker_id: str | None = None
        self._previous_inbound_was_bare_ack = False
        self._activation_seconds = timing.activation_seconds
        self._barge_in_min_words = interaction.barge_in_min_words
        self._max_spoken_sentences = generation.max_spoken_sentences
        self._language = generation.language
        self._sentence_streaming = generation.sentence_streaming
        self._stream_stops = 0
        self._barge_in = interaction.barge_in
        self._clock = timing.clock
        self._active_until = 0.0
        self._session: MeetingAudio | None = None
        self._phase: ConversationPhase = "listening"
        self._events: list[TranscriptEvent] = []
        self._pending: deque[TranscriptEvent] = deque()
        self._seen: set[str] = set()
        self._generation = 0
        self._task: asyncio.Task[None] | None = None
        self._active_turn: asyncio.Task[None] | None = None
        self._active_request_id = ""
        self._active_transcript_id = ""
        self._active_generation = 0
        self._active_started = 0.0
        self._failure: ConversationError | None = None
        self._closed = False
        self._interrupted = False
        self._last_interim: str | None = None
        self._last_interim_text: str | None = None
        self._pre_generate_enabled = generation.pre_generate_enabled
        self._pregen_fuzzy_restart_enabled = generation.pregen_fuzzy_restart_enabled
        self._endpoint = SemanticEndpoint()
        self._pregen: _Pregeneration | None = None
        self._pregen_count = 0
        self._vad_stopped: bool = False

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

    async def _discard_rejected(self, event: TranscriptEvent, *, bare_ack: bool) -> bool:
        if self._participant_policy_enabled:
            if self._policy_allows(event):
                return False
        elif self._wake_phrases and not self._addressed(event):
            pass
        elif bare_ack and self._previous_inbound_was_bare_ack:
            self._record(
                "ack_decided", transcript_id=event.id, action="silent", reason="bare_ack_run"
            )
            self._previous_inbound_was_bare_ack = True
        else:
            return False
        await self._discard_pregen("rejected")
        return True

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
        self._endpoint.reset()
        self._vad_stopped = False
        self._last_interim_text = None
        bare_ack = _is_bare_ack(event.text)
        if await self._discard_rejected(event, bare_ack=bare_ack):
            return
        if not self._participant_policy_enabled:
            self._previous_inbound_was_bare_ack = bare_ack
        if await self._maybe_adopt_pregen(event):
            return
        self._pending.append(event)
        self._generation += 1
        if self._task is not None and not self._task.done():
            self._move("interrupted", event.id)
        await self._cancel_active_turn("new_final")
        if self._session is not None and self._barge_in and self._session.is_speaking:
            self._record("barge_in_detected", trigger="final", words=len(event.text.split()))
        if self._session is not None and (
            (self._barge_in and self._session.is_speaking)
            or (self.is_busy and not self._session.is_speaking)
        ):
            await self._session.stop_playback()
        self._start_pending()

    async def _advance_pregen(self, text: str) -> None:
        self._last_interim_text = text
        await self._observe_pregen(text)
        if not (self._vad_stopped and self._pregen_gates_pass()):
            return
        fresh = normalize_words(text)
        pregen = self._pregen
        if pregen is None:
            if fresh:
                self._begin_pregen(text)
        elif fresh and fresh != pregen.words:
            if not self._maybe_suppress_pregen_restart(pregen, fresh):
                await self._discard_pregen("superseded", fresh)
                self._begin_pregen(text)

    async def handle_interim(self, text: str) -> None:
        await self._advance_pregen(text)
        session = self._session
        if (
            self._closed
            or self._failure is not None
            or not self._barge_in
            or session is None
            or (self._wake_phrases and self._clock() >= self._active_until)
        ):
            return
        words = len(text.split())
        if words < self._barge_in_min_words:
            return
        child = self._active_turn
        if not session.is_speaking and (child is None or child.done()):
            return
        if text == self._last_interim:
            return
        self._last_interim = text
        if not self._interrupted:
            self._interrupted = True
            self._record("barge_in_detected", trigger="interim", words=words)
            if session.is_speaking:
                await session.stop_playback()
                self._stream_stops += 1
        await self._cancel_active_turn("interim")

    def _pregen_gates_pass(self) -> bool:
        session = self._session
        if self._closed or self._failure is not None:
            return False
        if session is None:
            return False
        if not self._pre_generate_enabled:
            return False
        if not self._sentence_streaming:
            return False
        if not isinstance(self._responder, StreamingConversationResponder):
            return False
        if self.is_busy or self._pending or session.is_speaking:
            return False
        child = self._active_turn
        if child is not None and not child.done():
            return False
        return True

    async def _observe_pregen(self, text: str) -> None:
        if not self._pregen_gates_pass():
            return
        if self._pregen is not None:
            return
        if not self._endpoint.observe(text):
            return
        self._begin_pregen(text)

    async def note_vad(self, *, active: bool) -> None:
        if active:
            self._vad_stopped = False
            return
        self._vad_stopped = True
        if not self._pregen_gates_pass():
            return
        if self._pregen is not None:
            return
        text = self._last_interim_text or ""
        if not self._endpoint.observe_stop(text, min_words=1):
            return
        self._begin_pregen(text)

    def _begin_pregen(self, text: str) -> None:
        self._pregen_count += 1
        interim = TranscriptEvent(
            id=f"pregen-{self._pregen_count}",
            timestamp=datetime.now(UTC).isoformat(),
            source="meeting_audio",
            text=text,
            finalized=False,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
        words = normalize_words(text)
        request_id = f"{interim.id}:{self._generation}"
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        pregen = _Pregeneration(
            words=words,
            request_id=request_id,
            started=self._clock(),
            generation=self._generation,
            queue=queue,
            buffered=[],
        )
        pregen.task = asyncio.create_task(self._buffer_pregen(pregen, (interim,)))
        self._pregen = pregen
        self._record("pregeneration_started", request_id=request_id, words=len(words))

    def _addressed(self, event: TranscriptEvent) -> bool:
        now = self._clock()
        if any(phrase in event.text.casefold() for phrase in self._wake_phrases):
            self._active_until = now + self._activation_seconds
            return True
        return now < self._active_until

    def _policy_allows(self, event: TranscriptEvent) -> bool:
        now = self._clock()
        addressed = is_vocative(event.text, self._wake_phrases)
        if addressed:
            self._active_until = now + self._activation_seconds
        activation_valid = now < self._active_until
        context = ParticipationContext(
            addressed=addressed,
            followup=not addressed and activation_valid,
            same_participant=same_observed_speaker(self._last_speaker_id, event.participant_id),
            ambiguous=False,
            others_speaking=False,
            activation_valid=activation_valid,
        )
        decision = choose_participation(context)
        self._record(
            "policy_decided",
            transcript_id=event.id,
            action=decision.action,
            reason=decision.reason,
        )
        if decision.action == "silent":
            return False
        if event.participant_id != "unknown":
            self._last_speaker_id = event.participant_id
        return True

    async def wait_idle(self) -> None:
        if self._task is not None:
            await asyncio.shield(self._task)
        self.raise_if_failed()

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._vad_stopped = False
        self._pending.clear()
        pregen = self._pregen
        if pregen is not None:
            await self._discard_pregen(_pregen_failure_reason(pregen.task) or "rejected")
        self._session = None
        await self._cancel_active_turn("close")
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._phase = "closed"

    async def _cancel_active_turn(self, reason: str) -> None:
        child = self._active_turn
        if child is None or child.done():
            return
        request_id = self._active_request_id
        transcript_id = self._active_transcript_id
        generation = self._active_generation
        started = self._active_started
        self._record(
            "agent_cancel_requested",
            request_id=request_id,
            transcript_id=transcript_id,
            generation=generation,
            reason=reason,
            duration_ms=(self._clock() - started) * 1000,
        )
        child.cancel()
        await asyncio.gather(child, return_exceptions=True)
        self._record(
            "agent_cancelled",
            request_id=request_id,
            transcript_id=transcript_id,
            generation=generation,
            reason=reason,
            duration_ms=(self._clock() - started) * 1000,
        )

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

    def _drop_outcome(self, generation: int) -> tuple[str, str]:
        if self._closed:
            return "closed", "close"
        if generation != self._generation:
            return "stale_generation", "new_final"
        return "stopped", "interim"

    def _record_drop(self, turn: StreamTurn, sentence_index: int, detail: str, reason: str) -> None:
        self._record(
            "sentence_dropped",
            request_id=turn.request_id,
            transcript_id=turn.event_id,
            sentence_index=sentence_index,
            detail=detail,
        )
        self._record(
            "agent_cancelled",
            request_id=turn.request_id,
            transcript_id=turn.event_id,
            generation=turn.generation,
            reason=reason,
            duration_ms=(self._clock() - turn.started) * 1000,
        )

    def _live_session(
        self, turn: StreamTurn, stop_mark: int, sentence_index: int
    ) -> MeetingAudio | None:
        if self._closed or turn.generation != self._generation or self._stream_stops != stop_mark:
            detail, reason = self._drop_outcome(turn.generation)
            self._record_drop(turn, sentence_index, detail, reason)
            return None
        session = self._session
        if session is None:
            self._record_drop(turn, sentence_index, "closed", "close")
            return None
        return session

    def _record_cancelled(self, turn: StreamTurn, spoken: int) -> None:
        if spoken:
            detail, _reason = self._drop_outcome(turn.generation)
            self._record(
                "sentence_dropped",
                request_id=turn.request_id,
                transcript_id=turn.event_id,
                sentence_index=spoken,
                detail=detail,
            )

    def _finish_turn(self, turn: StreamTurn, spoken: int) -> None:
        self.raise_if_failed()
        if turn.generation != self._generation or self._closed:
            return
        if not spoken:
            self._move("waiting", turn.event_id)
        else:
            self._move("listening", turn.event_id)

    async def _speak_streamed(
        self,
        responder: StreamingConversationResponder,
        events: tuple[TranscriptEvent, ...],
        turn: StreamTurn,
    ) -> None:
        stop_mark = self._stream_stops
        spoken = 0
        try:
            async with asyncio.timeout(self._response_timeout):
                stream = responder.stream_sentences(
                    events,
                    partial(self._tool_started, turn.generation, turn.event_id),
                    request_id=turn.request_id,
                )
                try:
                    async for sentence in stream:
                        session = self._live_session(turn, stop_mark, spoken)
                        if session is None:
                            return
                        if not spoken:
                            self._move("queued", turn.event_id)
                        self.raise_if_failed()
                        self._interrupted = False
                        self._last_interim = None
                        await session.speak(
                            sentence, sentence_index=spoken, transcript_id=turn.event_id
                        )
                        spoken += 1
                finally:
                    close = getattr(stream, "aclose", None)
                    if close is not None:
                        with suppress(Exception):
                            await close()
        except asyncio.CancelledError:
            self._record_cancelled(turn, spoken)
            raise
        except BaseException:
            self._record(
                "agent_failed",
                request_id=turn.request_id,
                duration_ms=(self._clock() - turn.started) * 1000,
            )
            raise
        else:
            self._record(
                "agent_finished",
                request_id=turn.request_id,
                duration_ms=(self._clock() - turn.started) * 1000,
            )
        self._finish_turn(turn, spoken)

    async def _maybe_adopt_pregen(self, event: TranscriptEvent) -> bool:
        pregen = self._pregen
        if pregen is None:
            return False
        reason = _pregen_failure_reason(pregen.task)
        if reason is not None:
            await self._discard_pregen(reason)
            return False
        final_words = normalize_words(event.text)
        if final_words != pregen.words:
            await self._discard_pregen("mismatched", final_words)
            return False
        session = self._session
        if session is None:
            await self._discard_pregen("error")
            return False
        self._pregen = None
        pregen.adopted = True
        if pregen.task is not None and pregen.task.done():
            pregen.queue.put_nowait(None)
        self._generation += 1
        self._pending.clear()
        generation = self._generation
        started = self._clock()
        request_id = f"{event.id}:{generation}"
        self._record(
            "pregeneration_finished",
            request_id=request_id,
            outcome="adopted",
            duration_ms=(started - pregen.started) * 1000,
            **describe_word_diff(pregen.words, final_words),
        )
        for index, sentence in enumerate(pregen.buffered):
            await session.speak(sentence, sentence_index=index, transcript_id=event.id)
        if pregen.buffered:
            self._move("queued", event.id)
        turn = StreamTurn(generation, event.id, request_id, started)
        worker = asyncio.create_task(self._consume_adopted(pregen, turn, len(pregen.buffered)))
        self._active_turn = worker
        self._active_request_id = request_id
        self._active_transcript_id = event.id
        self._active_generation = generation
        self._active_started = started
        self._task = asyncio.create_task(self._await_adopted(worker))
        return True

    async def _buffer_pregen(
        self, pregen: _Pregeneration, events: tuple[TranscriptEvent, ...]
    ) -> None:
        responder = self._responder
        if not isinstance(responder, StreamingConversationResponder):
            return
        stream = responder.stream_sentences(
            events,
            partial(self._tool_started, pregen.generation, pregen.request_id),
            request_id=pregen.request_id,
        )
        pregen.stream = stream
        try:
            async with asyncio.timeout(self._response_timeout):
                async for sentence in stream:
                    if pregen.adopted:
                        pregen.queue.put_nowait(sentence)
                    else:
                        pregen.buffered.append(sentence)
        finally:
            if pregen.adopted:
                pregen.queue.put_nowait(None)
            close = getattr(stream, "aclose", None)
            if close is not None:
                with suppress(Exception):
                    await close()

    async def _close_adopted(self, pregen: _Pregeneration) -> None:
        close = getattr(pregen.stream, "aclose", None)
        if close is not None:
            with suppress(Exception):
                await close()
        task = pregen.task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _consume_adopted(
        self,
        pregen: _Pregeneration,
        turn: StreamTurn,
        offset: int,
    ) -> None:
        stop_mark = self._stream_stops
        spoken = offset
        try:
            async with asyncio.timeout(self._response_timeout):
                while True:
                    sentence = await pregen.queue.get()
                    if sentence is None:
                        break
                    session = self._live_session(turn, stop_mark, spoken)
                    if session is None:
                        return
                    if not spoken:
                        self._move("queued", turn.event_id)
                    self.raise_if_failed()
                    self._interrupted = False
                    self._last_interim = None
                    await session.speak(
                        sentence, sentence_index=spoken, transcript_id=turn.event_id
                    )
                    spoken += 1
        except asyncio.CancelledError:
            self._record_cancelled(turn, spoken)
            raise
        except BaseException:
            self._record(
                "agent_failed",
                request_id=turn.request_id,
                duration_ms=(self._clock() - turn.started) * 1000,
            )
            raise
        else:
            self._record(
                "agent_finished",
                request_id=turn.request_id,
                duration_ms=(self._clock() - turn.started) * 1000,
            )
        finally:
            await self._close_adopted(pregen)
        self._finish_turn(turn, spoken)

    async def _await_adopted(self, worker: asyncio.Task[None]) -> None:
        try:
            await worker
        except asyncio.CancelledError:
            if self._closed:
                raise
            self.raise_if_failed()
        except Exception as error:
            self._fail(error)
        finally:
            if self._active_turn is worker:
                self._active_turn = None
        if not self._closed and self._failure is None and self._pending:
            await self._respond_pending()

    def _maybe_suppress_pregen_restart(
        self, pregen: _Pregeneration, fresh: tuple[str, ...]
    ) -> bool:
        if not self._pregen_fuzzy_restart_enabled:
            return False
        stats = describe_word_diff(pregen.words, fresh)
        dist = int(stats["dist"])
        words_pre = int(stats["words_pre"])
        words_final = int(stats["words_final"])
        if not (
            (stats["diff_class"] == "prefix_extend" and words_final - words_pre <= 2)
            or (dist <= 1 and max(words_pre, words_final) >= 6)
        ):
            return False
        pregen.words = fresh
        self._record("pregeneration_restart_skipped", request_id=pregen.request_id, **stats)
        return True

    async def _discard_pregen(self, reason: str, compared: tuple[str, ...] | None = None) -> None:
        pregen = self._pregen
        if pregen is None:
            return
        self._pregen = None
        task = pregen.task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        diff: dict[str, float | int | str] = (
            describe_word_diff(pregen.words, compared) if compared is not None else {}
        )
        self._record(
            "pregeneration_finished",
            request_id=pregen.request_id,
            outcome="discarded",
            reason=reason,
            **diff,
        )

    async def _run_turn(
        self,
        events: tuple[TranscriptEvent, ...],
        generation: int,
        event_id: str,
        request_id: str,
        started: float,
    ) -> None:
        responder = self._responder
        if self._sentence_streaming and isinstance(responder, StreamingConversationResponder):
            turn = StreamTurn(generation, event_id, request_id, started)
            await self._speak_streamed(responder, events, turn)
            return
        try:
            async with asyncio.timeout(self._response_timeout):
                reply = await cast(ConversationResponder, responder).respond(
                    events, partial(self._tool_started, generation, event_id)
                )
        except Exception:
            self._record(
                "agent_failed",
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
        session = self._session
        if not reply.strip() or session is None:
            self._move("waiting", event_id)
            return
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
        self._last_interim = None
        await session.speak(spoken)
        if generation == self._generation and not self._closed:
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
                self._interrupted = False
                child = asyncio.create_task(
                    self._run_turn(events, generation, event_id, request_id, started)
                )
                self._active_turn = child
                self._active_request_id = request_id
                self._active_transcript_id = event_id
                self._active_generation = generation
                self._active_started = started
                try:
                    await child
                except asyncio.CancelledError:
                    if self._closed:
                        raise
                    self.raise_if_failed()
                finally:
                    if self._active_turn is child:
                        self._active_turn = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
