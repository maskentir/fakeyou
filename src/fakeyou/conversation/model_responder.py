from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.sentence_stream import SentenceStream, model_text
from fakeyou.conversation.turn_events import JournalSink
from fakeyou.foundation.transcript import TranscriptEvent


class ToolPhase(Protocol):
    @property
    def pending(self) -> bool: ...

    @property
    def boundary(self) -> int: ...


ModelChunks = Callable[[str], AsyncIterator[tuple[object, object]]]


@dataclass(frozen=True, slots=True)
class ReplyTrace:
    journal: JournalSink | None = None
    request_id: str = ""
    transcript_id: str | None = None
    on_tool_start: Callable[[], None] | None = None


class _AnswerJournal:
    def __init__(self, trace: ReplyTrace) -> None:
        self._trace = trace
        self._first_text = False
        self._observed_chars = 0
        self._spoken_chars = 0

    def note_piece(self, piece: str) -> None:
        self._observed_chars += len(piece)
        if not self._first_text and piece.strip():
            self._first_text = True
            if self._trace.journal is not None:
                self._trace.journal.record(
                    "answer_text_emitted",
                    source="llm",
                    request_id=self._trace.request_id,
                    transcript_id=self._trace.transcript_id,
                )

    def note_sentence(self, sentence: str, index: int, *, spoken: bool) -> None:
        if self._trace.journal is not None:
            self._trace.journal.record(
                "answer_sentence_emitted",
                source="agent",
                request_id=self._trace.request_id,
                transcript_id=self._trace.transcript_id,
                sentence_index=index,
                chars=len(sentence),
            )
        if spoken:
            self._spoken_chars += len(sentence) + (1 if self._spoken_chars else 0)

    def note_cap(self) -> None:
        if self._trace.journal is not None:
            self._trace.journal.record(
                "reply_capped",
                source="agent",
                request_id=self._trace.request_id,
                transcript_id=self._trace.transcript_id,
                chars_in=self._observed_chars,
                chars_out=self._spoken_chars,
            )


class ModelResponder:
    def __init__(
        self,
        *,
        chunks: ModelChunks,
        phase: ToolPhase,
        max_sentences: int,
        language: str,
        trace: ReplyTrace | None = None,
    ) -> None:
        self._chunks = chunks
        self._phase = phase
        self._max_sentences = max_sentences
        self._language = language
        self._trace = ReplyTrace() if trace is None else trace

    def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        return self._sentences(transcript)

    async def _sentences(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        stream = SentenceStream(self._max_sentences, self._language)
        answer = _AnswerJournal(self._trace)
        tool_active = False
        async for message, metadata in self._chunks(transcript):
            if self._phase.pending and not tool_active and self._trace.on_tool_start is not None:
                self._trace.on_tool_start()
            tool_active = self._phase.pending
            if not _is_model_chunk(metadata):
                continue
            stream.observe_boundary(self._phase.boundary)
            piece = model_text(message, metadata, tool_pending=self._phase.pending)
            if not piece:
                continue
            answer.note_piece(piece)
            base = stream.yielded
            for offset, sentence in enumerate(stream.take(piece)):
                answer.note_sentence(sentence, base + offset, spoken=True)
                yield SpokenChunk(text=sentence, language=self._language)
            if stream.check_cap():
                answer.note_cap()
                return
        tail = stream.flush()
        if tail is not None:
            answer.note_sentence(tail, stream.yielded, spoken=False)
            yield SpokenChunk(text=tail, language=self._language)


class WholeReplier(Protocol):
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk: ...


class ReplierWholeResponder:
    def __init__(
        self,
        replier: WholeReplier,
        events: tuple[TranscriptEvent, ...],
        on_tool_start: Callable[[], None],
    ) -> None:
        self._replier = replier
        self._events = events
        self._on_tool_start = on_tool_start

    async def respond(self, _transcript: str) -> SpokenChunk:
        return await self._replier.respond(self._events, self._on_tool_start)


class SentenceSource(Protocol):
    def __call__(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[SpokenChunk]: ...


class ReplierStreamResponder:
    def __init__(
        self,
        source: SentenceSource,
        events: tuple[TranscriptEvent, ...],
        on_tool_start: Callable[[], None],
        request_id: str,
    ) -> None:
        self._source = source
        self._events = events
        self._on_tool_start = on_tool_start
        self._request_id = request_id

    def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        return self._source(self._events, self._on_tool_start, request_id=self._request_id)


def _is_model_chunk(metadata: object) -> bool:
    return isinstance(metadata, dict) and metadata.get("langgraph_node") == "model"
