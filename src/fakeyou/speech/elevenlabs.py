import asyncio
import base64
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import suppress
from functools import partial
from typing import Any
from urllib.parse import urlencode

from websockets.exceptions import WebSocketException

from fakeyou.config.settings import DEFAULTS, ElevenLabsTTS
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault, classify_http
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from fakeyou.speech.streaming import SpeechTag, StreamedSegment, journaled_stream
from fakeyou.speech.websocket import (
    MALFORMED,
    SentenceAudio,
    SocketDialect,
    SpeechConnection,
    SpeechSocket,
    close_code,
    connect_socket,
    lost,
    secure_scheme,
    silent,
)

INACTIVITY_TIMEOUT_SECONDS = 180
QUIET_END_SECONDS = 2.0
LANGUAGE_BLIND_MODELS = ("eleven_multilingual_v1", "eleven_multilingual_v2", "eleven_monolingual")
NORMAL_CLOSE = 1000
SERVER_ERROR_CLOSE = 1011
POLICY_CLOSE = 1008
FAULT_STATUSES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("quota", "credit", "payment"), 402),
    (("api key", "api_key", "unauthorized"), 401),
    (("max_active", "too many requests", "rate limit", "concurrent"), 429),
)

LanguageOpener = Callable[[str | None], Awaitable[SpeechSocket]]


def takes_language(model: str) -> bool:
    return not model.startswith(LANGUAGE_BLIND_MODELS)


def socket_url(settings: ElevenLabsTTS, language: str | None) -> str:
    fields: dict[str, object] = {
        "model_id": settings.model,
        "output_format": f"pcm_{PLAYBACK_SAMPLE_RATE}",
        "sync_alignment": "true",
        "inactivity_timeout": INACTIVITY_TIMEOUT_SECONDS,
    }
    if language is not None:
        fields["language_code"] = language
    query = urlencode(fields)
    base = secure_scheme(settings.base_url)
    return f"{base}/v1/text-to-speech/{settings.voice}/multi-stream-input?{query}"


async def connect_elevenlabs(
    settings: ElevenLabsTTS, api_key: str, language: str | None
) -> SpeechSocket:
    return await connect_socket(socket_url(settings, language), {"xi-api-key": api_key})


def _detail(message: dict[str, Any]) -> str:
    for field in ("message", "error", "detail"):
        if message.get(field):
            return str(message[field])
    return "elevenlabs error"


def _fault(detail: str, code: int | None) -> ProviderFault:
    lowered = detail.casefold()
    for words, status in FAULT_STATUSES:
        if any(word in lowered for word in words):
            return classify_http("tts", status, detail)
    return classify_http("tts", 400 if code == POLICY_CLOSE else 500, detail)


def _socket_fault(message: dict[str, Any]) -> ProviderFault | None:
    if message.get("audio") or not (message.get("error") or message.get("message")):
        return None
    return _fault(_detail(message), None)


def _closed_fault(error: WebSocketException | OSError) -> ProviderFault:
    code, reason = close_code(error)
    if code is None or code == NORMAL_CLOSE:
        return lost(f"websocket receive failed: {type(error).__name__}")
    return _fault(reason or f"websocket closed with {code}", code)


def _context_of(message: dict[str, Any]) -> object:
    return message.get("contextId") or message.get("context_id")


ELEVENLABS_DIALECT = SocketDialect(
    context_of=_context_of, socket_fault=_socket_fault, closed_fault=_closed_fault
)


class _Spelling:
    def __init__(self, words: list[WordTiming]) -> None:
        self._words = words
        self._letters: list[str] = []
        self._start = 0.0
        self._end = 0.0

    def add(self, alignment: dict[str, Any], offset: float) -> None:
        for char, start_ms, duration_ms in zip(
            alignment.get("chars") or (),
            alignment.get("charStartTimesMs") or (),
            alignment.get("charDurationsMs") or (),
            strict=False,
        ):
            start = offset + float(start_ms) / 1000
            if str(char).isspace():
                self.finish()
                continue
            if not self._letters:
                self._start = start
            self._letters.append(str(char))
            self._end = start + float(duration_ms) / 1000

    def finish(self) -> None:
        if self._letters:
            self._words.append(WordTiming("".join(self._letters), self._start, self._end))
            self._letters = []


class _SentenceContext:
    def __init__(self, connection: SpeechConnection) -> None:
        self.context_id = uuid.uuid4().hex
        self.socket: SpeechSocket | None = None
        self.closed = False
        self.sentence = SentenceAudio()
        self._connection = connection
        self._spelling = _Spelling(self.sentence.words)
        self._quiet: asyncio.TimerHandle | None = None

    def open(self, text: str) -> None:
        for message in (
            {"text": " ", "context_id": self.context_id},
            {"text": f"{text.strip()} ", "context_id": self.context_id, "flush": True},
            {"context_id": self.context_id, "close_context": True},
        ):
            self._connection.send(self, message)

    async def stream(self) -> AsyncGenerator[bytes]:
        try:
            async for chunk in self.sentence.pcm():
                yield chunk
        finally:
            self.cancel()

    def cancel(self) -> None:
        if self.closed:
            return
        self._connection.control(self, {"context_id": self.context_id, "close_context": True})
        self._close(None)

    def receive(self, message: dict[str, Any]) -> None:
        if self.closed:
            return
        audio = message.get("audio")
        if audio:
            offset = self.sentence.seconds
            self.sentence.feed(base64.b64decode(audio))
            self._spell(message.get("alignment"), offset)
            self._expect_more()
        if message.get("isFinal") or message.get("is_final"):
            self._close(None)
        elif not audio and (message.get("error") or message.get("message")):
            self._close(_fault(_detail(message), None))

    def lose(self, fault: ProviderFault) -> None:
        self._close(fault)

    def _spell(self, alignment: object, offset: float) -> None:
        if isinstance(alignment, dict):
            with suppress(*MALFORMED):
                self._spelling.add(alignment, offset)

    def _expect_more(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
        self._quiet = asyncio.get_running_loop().call_later(QUIET_END_SECONDS, self._close, None)

    def _close(self, fault: ProviderFault | None) -> None:
        if self.closed:
            return
        self.closed = True
        if self._quiet is not None:
            self._quiet.cancel()
        self._spelling.finish()
        self.sentence.end(fault)
        self._connection.forget(self)


class _ElevenLabsTurn:
    def __init__(
        self,
        connection: SpeechConnection,
        *,
        transcript_id: str | None,
        journal: Journal | None,
    ) -> None:
        self._connection = connection
        self._transcript_id = transcript_id
        self._journal = journal
        self._contexts: list[_SentenceContext] = []

    def speak(self, text: str, *, sentence_index: int | None = None) -> SpeechSegment:
        tag = SpeechTag("elevenlabs", sentence_index, self._transcript_id)
        if not text.strip():
            return StreamedSegment(journaled_stream(self._journal, text, silent, tag))
        context = _SentenceContext(self._connection)
        context.open(text)
        self._contexts = [*self._open_contexts(), context]
        chunks = journaled_stream(self._journal, text, lambda _text: context.stream(), tag)
        return StreamedSegment(chunks, words=context.sentence.words)

    async def finish(self) -> None:
        self._contexts = list(self._open_contexts())

    async def cancel(self) -> None:
        contexts, self._contexts = self._contexts, []
        for context in contexts:
            context.cancel()

    def _open_contexts(self) -> Sequence[_SentenceContext]:
        return [context for context in self._contexts if not context.closed]


class ElevenLabsSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        settings: ElevenLabsTTS,
        language: str = DEFAULTS.speech.language,
        journal: Journal | None = None,
        opener: LanguageOpener | None = None,
    ) -> None:
        self._model = settings.model
        self._language = language
        self._journal = journal
        self._opener = opener or partial(connect_elevenlabs, settings, api_key)
        self._takes_language = takes_language(settings.model)
        self._connections: dict[str | None, SpeechConnection] = {}
        self._closed = False

    @property
    def model(self) -> str:
        return self._model

    @property
    def journal(self) -> Journal | None:
        return self._journal

    def warm(self) -> None:
        self._connection(self._language).warm()

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return _ElevenLabsTurn(
            self._connection(language or self._language),
            transcript_id=transcript_id,
            journal=self._journal,
        )

    async def aclose(self) -> None:
        self._closed = True
        connections, self._connections = self._connections, {}
        for connection in connections.values():
            await connection.close()

    def _connection(self, language: str) -> SpeechConnection:
        key = language if self._takes_language else None
        connection = self._connections.get(key)
        if connection is None:
            connection = SpeechConnection(
                partial(self._opener, key), ELEVENLABS_DIALECT, closed=self._closed
            )
            if not self._closed:
                self._connections[key] = connection
        return connection
