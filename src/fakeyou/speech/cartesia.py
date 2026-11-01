import base64
import uuid
from collections import deque
from functools import partial
from typing import Any

from fakeyou.config.settings import DEFAULTS, CartesiaTTS
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_http
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from fakeyou.speech.streaming import SpeechTag, StreamedSegment, journaled_stream
from fakeyou.speech.websocket import (
    BYTES_PER_SECOND,
    SentenceAudio,
    SocketDialect,
    SocketOpener,
    SpeechConnection,
    SpeechSocket,
    connect_socket,
    secure_scheme,
    silent,
)

CARTESIA_API_VERSION = "2026-03-01"


async def connect_cartesia(url: str, api_key: str) -> SpeechSocket:
    return await connect_socket(url, {"X-API-Key": api_key})


def socket_url(base_url: str) -> str:
    return f"{secure_scheme(base_url)}/tts/websocket?cartesia_version={CARTESIA_API_VERSION}"


def _socket_fault(message: dict[str, Any]) -> ProviderFault | None:
    return _error_fault(message) if message.get("type") == "error" else None


CARTESIA_DIALECT = SocketDialect(
    context_of=lambda message: message.get("context_id"), socket_fault=_socket_fault
)


class CartesiaSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        settings: CartesiaTTS | None = None,
        language: str = DEFAULTS.speech.language,
        journal: Journal | None = None,
        opener: SocketOpener | None = None,
    ) -> None:
        resolved = settings if settings is not None else CartesiaTTS()
        self._model = resolved.model
        self._voice = resolved.voice
        self._language = language
        self._journal = journal
        self._connection = SpeechConnection(
            opener or partial(connect_cartesia, socket_url(resolved.base_url), api_key),
            CARTESIA_DIALECT,
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def journal(self) -> Journal | None:
        return self._journal

    def warm(self) -> None:
        self._connection.warm()

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return _CartesiaTurn(
            self._connection,
            self._request(language or self._language),
            transcript_id=transcript_id,
            journal=self._journal,
        )

    async def aclose(self) -> None:
        await self._connection.close()

    def _request(self, language: str) -> dict[str, Any]:
        return {
            "model_id": self._model,
            "voice": {"mode": "id", "id": self._voice},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": PLAYBACK_SAMPLE_RATE,
            },
            "language": language,
            "max_buffer_delay_ms": 0,
            "add_timestamps": True,
        }


def _error_fault(message: dict[str, Any]) -> ProviderFault:
    status = message.get("status_code")
    return classify_http("tts", int(status) if isinstance(status, int) else 500, _detail(message))


class _Sentence(SentenceAudio):
    def __init__(self) -> None:
        super().__init__()
        self.flush_id: Any = None


class _Context:
    def __init__(self, connection: SpeechConnection, request: dict[str, Any]) -> None:
        self.context_id = uuid.uuid4().hex
        self.socket: SpeechSocket | None = None
        self._connection = connection
        self._request = request
        self._pending: deque[_Sentence] = deque()
        self._spoken_bytes = 0
        self.closed = False

    def speak(self, text: str) -> _Sentence:
        sentence = _Sentence()
        self._pending.append(sentence)
        self._connection.send(self, self._message(transcript=text, more=True, flush=True))
        return sentence

    def finish(self) -> None:
        if not self.closed:
            self._connection.send(self, self._message(transcript="", more=False, flush=False))

    def cancel(self) -> None:
        if self.closed:
            return
        self._connection.control(self, {"context_id": self.context_id, "cancel": True})
        self._close(None)

    def receive(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        kind = message.get("type")
        if kind == "chunk":
            self._chunk(message)
        elif kind == "timestamps":
            self._timestamps(message.get("word_timestamps") or {})
        elif kind == "flush_done":
            self._flush_done(message.get("flush_id"))
        elif kind == "error":
            self._close(_error_fault(message))
        if message.get("done") and kind != "error":
            self._close(None)

    def lose(self, fault: ProviderFault) -> None:
        self._close(fault)

    def _chunk(self, message: dict[str, Any]) -> None:
        sentence = self._sentence_for(message.get("flush_id"))
        if sentence is not None:
            sentence.feed(base64.b64decode(message.get("data") or ""))

    def _timestamps(self, stamps: dict[str, Any]) -> None:
        if not self._pending:
            return
        offset = self._spoken_bytes / BYTES_PER_SECOND
        self._pending[0].words.extend(
            WordTiming(str(word), float(start) - offset, float(end) - offset)
            for word, start, end in zip(
                stamps.get("words") or (),
                stamps.get("start") or (),
                stamps.get("end") or (),
                strict=False,
            )
        )

    def _flush_done(self, flush_id: Any) -> None:
        sentence = self._sentence_for(flush_id)
        if sentence is None:
            return
        while self._pending:
            done = self._pending.popleft()
            self._spoken_bytes += done.audio_bytes
            done.end()
            if done is sentence:
                return

    def _sentence_for(self, flush_id: Any) -> _Sentence | None:
        for sentence in self._pending:
            if flush_id is not None and sentence.flush_id == flush_id:
                return sentence
        for sentence in self._pending:
            if sentence.flush_id is None:
                sentence.flush_id = flush_id
                return sentence
        return None

    def _message(self, *, transcript: str, more: bool, flush: bool) -> dict[str, Any]:
        return {
            **self._request,
            "context_id": self.context_id,
            "transcript": transcript,
            "continue": more,
            "flush": flush,
        }

    def _close(self, fault: ProviderFault | None) -> None:
        if self.closed:
            return
        self.closed = True
        pending, self._pending = self._pending, deque()
        for sentence in pending:
            sentence.end(fault)
        self._connection.forget(self)


def _detail(message: dict[str, Any]) -> str:
    for field in ("message", "error", "title"):
        if message.get(field):
            return str(message[field])
    return "cartesia error"


class _CartesiaTurn:
    def __init__(
        self,
        connection: SpeechConnection,
        request: dict[str, Any],
        *,
        transcript_id: str | None,
        journal: Journal | None,
    ) -> None:
        self._connection = connection
        self._request = request
        self._transcript_id = transcript_id
        self._journal = journal
        self._context: _Context | None = None

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        tag = SpeechTag("cartesia", sentence_index, self._transcript_id)
        if not text.strip():
            return StreamedSegment(journaled_stream(self._journal, text, silent, tag))
        context = self._context
        if context is None or context.closed:
            context = self._context = _Context(self._connection, self._request)
        sentence = context.speak(text)
        chunks = journaled_stream(self._journal, text, lambda _text: sentence.pcm(), tag)
        return StreamedSegment(chunks, words=sentence.words)

    async def finish(self) -> None:
        if self._context is not None:
            self._context.finish()

    async def cancel(self) -> None:
        if self._context is not None:
            self._context.cancel()
