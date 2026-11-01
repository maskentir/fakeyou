import asyncio
import base64
import json
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from functools import partial
from typing import Any, Protocol

import websockets
from websockets.exceptions import InvalidStatus, WebSocketException

from fakeyou.config.settings import DEFAULTS, CartesiaTTS
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault, classify_http
from fakeyou.speech.contract import SpeechSegment, SpeechTurn, WordTiming
from fakeyou.speech.streaming import SpeechTag, StreamedSegment, journaled_stream

CARTESIA_API_VERSION = "2026-03-01"
HANDSHAKE_TIMEOUT_SECONDS = 10.0
BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES


class CartesiaSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


SocketOpener = Callable[[], Awaitable[CartesiaSocket]]


async def connect_cartesia(url: str, api_key: str) -> CartesiaSocket:
    try:
        return await websockets.connect(
            url,
            additional_headers={"X-API-Key": api_key},
            open_timeout=HANDSHAKE_TIMEOUT_SECONDS,
        )
    except InvalidStatus as error:
        raise classify_http("tts", error.response.status_code, "websocket handshake") from error
    except (WebSocketException, OSError, TimeoutError) as error:
        raise _lost("websocket connect failed") from error


def _lost(detail: str) -> ProviderFault:
    return ProviderFault(
        "tts", "unavailable", evidence=FaultEvidence(detail=detail), retryable=True
    )


def socket_url(base_url: str) -> str:
    scheme, _, rest = base_url.partition("://")
    secure = {"https": "wss", "http": "ws"}.get(scheme, scheme)
    return f"{secure}://{rest}/tts/websocket?cartesia_version={CARTESIA_API_VERSION}"


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
        self._connection = _Connection(
            opener or partial(connect_cartesia, socket_url(resolved.base_url), api_key)
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


MALFORMED = (TypeError, ValueError, AttributeError, KeyError)


class _Connection:
    def __init__(self, opener: SocketOpener) -> None:
        self._opener = opener
        self._socket: CartesiaSocket | None = None
        self._reader: asyncio.Task[None] | None = None
        self._outbox: asyncio.Queue[tuple[_Context, dict[str, Any]]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._contexts: dict[str, _Context] = {}
        self._warm = False
        self._closed = False

    def warm(self) -> None:
        self._warm = True
        with suppress(RuntimeError):
            asyncio.get_running_loop()
            self._start_writer()

    def send(self, context: "_Context", message: dict[str, Any]) -> None:
        if self._closed:
            context.lose(_lost("the speech connection is closed"))
            return
        if not context.closed:
            self._contexts[context.context_id] = context
        self._start_writer()
        self._outbox.put_nowait((context, message))

    def forget(self, context: "_Context") -> None:
        self._contexts.pop(context.context_id, None)

    async def close(self) -> None:
        self._closed = True
        socket, self._socket = self._socket, None
        for task in (self._writer, self._reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._lose(_lost("the speech connection is closed"), on=None)
        if socket is not None:
            with suppress(WebSocketException, OSError):
                await socket.close()

    def _start_writer(self) -> None:
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._write_loop())

    async def _write_loop(self) -> None:
        if self._warm:
            with suppress(ProviderFault):
                await self._ensure()
        while True:
            context, message = await self._outbox.get()
            if message.get("cancel"):
                await self._send_cancel(message)
            elif not context.closed:
                await self._deliver(context, message)

    async def _deliver(self, context: "_Context", message: dict[str, Any]) -> None:
        try:
            socket = await self._ensure()
            context.socket = socket
            await socket.send(json.dumps(message))
        except ProviderFault as fault:
            context.lose(fault)
        except (WebSocketException, OSError) as error:
            await self._drop_socket()
            context.lose(_lost(f"websocket send failed: {type(error).__name__}"))

    async def _send_cancel(self, message: dict[str, Any]) -> None:
        socket = self._socket
        if socket is not None:
            with suppress(WebSocketException, OSError):
                await socket.send(json.dumps(message))

    async def _ensure(self) -> CartesiaSocket:
        if self._socket is None:
            socket = await self._opener()
            self._socket = socket
            self._reader = asyncio.create_task(self._read_loop(socket))
        return self._socket

    async def _read_loop(self, socket: CartesiaSocket) -> None:
        reason = "websocket closed"
        try:
            while True:
                self._route(socket, await socket.recv())
        except (WebSocketException, OSError) as error:
            reason = f"websocket receive failed: {type(error).__name__}"
        finally:
            if self._socket is socket:
                self._socket = None
            self._lose(_lost(reason), on=socket)

    def _route(self, socket: CartesiaSocket, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        context = self._contexts.get(str(message.get("context_id")))
        if context is None:
            if message.get("type") == "error":
                self._lose(_error_fault(message), on=socket)
            return
        try:
            context.receive(message)
        except MALFORMED as error:
            context.lose(_lost(f"malformed {message.get('type')} message: {type(error).__name__}"))

    def _lose(self, fault: ProviderFault, *, on: CartesiaSocket | None) -> None:
        for context in list(self._contexts.values()):
            if on is None or context.socket is on:
                context.lose(fault)

    async def _drop_socket(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            with suppress(WebSocketException, OSError):
                await socket.close()


def _error_fault(message: dict[str, Any]) -> ProviderFault:
    status = message.get("status_code")
    return classify_http("tts", int(status) if isinstance(status, int) else 500, _detail(message))


class _Sentence:
    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes | ProviderFault | None] = asyncio.Queue()
        self.words: list[WordTiming] = []
        self.audio_bytes = 0
        self.detached = False
        self.flush_id: Any = None

    def feed(self, pcm: bytes) -> None:
        self.audio_bytes += len(pcm)
        if not self.detached:
            self.chunks.put_nowait(pcm)

    def end(self, fault: ProviderFault | None = None) -> None:
        self.chunks.put_nowait(fault)

    async def pcm(self) -> AsyncGenerator[bytes]:
        try:
            while True:
                item = await self.chunks.get()
                if item is None:
                    return
                if isinstance(item, ProviderFault):
                    raise item
                yield item
        finally:
            self.detached = True


class _Context:
    def __init__(self, connection: _Connection, request: dict[str, Any]) -> None:
        self.context_id = uuid.uuid4().hex
        self.socket: CartesiaSocket | None = None
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
        self._connection.send(self, {"context_id": self.context_id, "cancel": True})
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


async def _silent(text: str) -> AsyncGenerator[bytes]:
    del text
    return
    yield b""


class _CartesiaTurn:
    def __init__(
        self,
        connection: _Connection,
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
            return StreamedSegment(journaled_stream(self._journal, text, _silent, tag))
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
