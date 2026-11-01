import asyncio
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault, classify_http
from fakeyou.speech.contract import WordTiming

HANDSHAKE_TIMEOUT_SECONDS = 10.0
REDIAL_AFTER_SECONDS = 10.0
BYTES_PER_SECOND = PLAYBACK_SAMPLE_RATE * PCM16_SAMPLE_BYTES
MALFORMED = (TypeError, ValueError, AttributeError, KeyError)


class SpeechSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


SocketOpener = Callable[[], Awaitable[SpeechSocket]]


def lost(detail: str) -> ProviderFault:
    return ProviderFault(
        "tts", "unavailable", evidence=FaultEvidence(detail=detail), retryable=True
    )


def secure_scheme(base_url: str) -> str:
    scheme, _, rest = base_url.partition("://")
    secure = {"https": "wss", "http": "ws"}.get(scheme, scheme)
    return f"{secure}://{rest}"


async def connect_socket(url: str, headers: Mapping[str, str]) -> SpeechSocket:
    try:
        return await websockets.connect(
            url, additional_headers=dict(headers), open_timeout=HANDSHAKE_TIMEOUT_SECONDS
        )
    except InvalidStatus as error:
        raise classify_http("tts", error.response.status_code, "websocket handshake") from error
    except (WebSocketException, OSError, TimeoutError) as error:
        raise lost("websocket connect failed") from error


class SocketContext(Protocol):
    @property
    def context_id(self) -> str: ...

    @property
    def closed(self) -> bool: ...

    socket: SpeechSocket | None

    def receive(self, message: dict[str, Any]) -> None: ...

    def lose(self, fault: ProviderFault) -> None: ...


def _receive_failed(error: WebSocketException | OSError) -> ProviderFault:
    return lost(f"websocket receive failed: {type(error).__name__}")


@dataclass(frozen=True, slots=True)
class SocketDialect:
    context_of: Callable[[dict[str, Any]], object]
    socket_fault: Callable[[dict[str, Any]], ProviderFault | None]
    closed_fault: Callable[[WebSocketException | OSError], ProviderFault] = _receive_failed


class SpeechConnection:
    def __init__(
        self, opener: SocketOpener, dialect: SocketDialect, *, closed: bool = False
    ) -> None:
        self._opener = opener
        self._dialect = dialect
        self._socket: SpeechSocket | None = None
        self._dialing = asyncio.Lock()
        self._redial: asyncio.Task[None] | None = None
        self._reader: asyncio.Task[None] | None = None
        self._outbox: asyncio.Queue[tuple[SocketContext, dict[str, Any], bool]] = asyncio.Queue()
        self._writer: asyncio.Task[None] | None = None
        self._contexts: dict[str, SocketContext] = {}
        self._warm = False
        self._closed = closed

    def warm(self) -> None:
        self._warm = True
        with suppress(RuntimeError):
            asyncio.get_running_loop()
            self._start_writer()

    def send(self, context: SocketContext, message: dict[str, Any]) -> None:
        if self._closed:
            context.lose(lost("the speech connection is closed"))
            return
        if not context.closed:
            self._contexts[context.context_id] = context
        self._start_writer()
        self._outbox.put_nowait((context, message, False))

    def control(self, context: SocketContext, message: dict[str, Any]) -> None:
        if self._closed:
            return
        self._start_writer()
        self._outbox.put_nowait((context, message, True))

    def forget(self, context: SocketContext) -> None:
        self._contexts.pop(context.context_id, None)

    async def close(self) -> None:
        self._closed = True
        socket, self._socket = self._socket, None
        for task in (self._writer, self._redial, self._reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._lose(lost("the speech connection is closed"), on=None)
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
            context, message, control = await self._outbox.get()
            if control:
                await self._send_control(context, message)
            elif not context.closed:
                await self._deliver(context, message)

    async def _deliver(self, context: SocketContext, message: dict[str, Any]) -> None:
        attempts = 2 if context.socket is None else 1
        for attempt in range(attempts):
            try:
                socket = await self._ensure()
                context.socket = socket
                await socket.send(json.dumps(message))
            except ProviderFault as fault:
                context.lose(fault)
            except (WebSocketException, OSError) as error:
                context.socket = None
                await self._drop_socket()
                if attempt + 1 < attempts:
                    continue
                context.lose(lost(f"websocket send failed: {type(error).__name__}"))
            return

    async def _send_control(self, context: SocketContext, message: dict[str, Any]) -> None:
        socket = self._socket
        if socket is not None and context.socket is socket:
            with suppress(WebSocketException, OSError):
                await socket.send(json.dumps(message))

    async def _ensure(self) -> SpeechSocket:
        async with self._dialing:
            if self._socket is None:
                socket = await self._opener()
                self._socket = socket
                self._reader = asyncio.create_task(self._read_loop(socket))
            return self._socket

    async def _read_loop(self, socket: SpeechSocket) -> None:
        opened = asyncio.get_running_loop().time()
        fault = lost("websocket closed")
        try:
            while True:
                self._route(socket, await socket.recv())
        except (WebSocketException, OSError) as error:
            fault = self._dialect.closed_fault(error)
        finally:
            if self._socket is socket:
                self._socket = None
            self._lose(fault, on=socket)
            lived = asyncio.get_running_loop().time() - opened
            if self._warm and not self._closed and lived >= REDIAL_AFTER_SECONDS:
                self._redial = asyncio.create_task(self._dial_quietly())

    async def _dial_quietly(self) -> None:
        with suppress(ProviderFault):
            await self._ensure()

    def _route(self, socket: SpeechSocket, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        context_id = self._dialect.context_of(message)
        if context_id is None:
            fault = self._dialect.socket_fault(message)
            if fault is not None:
                self._lose(fault, on=socket)
            return
        context = self._contexts.get(str(context_id))
        if context is None:
            return
        try:
            context.receive(message)
        except MALFORMED as error:
            context.lose(lost(f"malformed speech message: {type(error).__name__}"))

    def _lose(self, fault: ProviderFault, *, on: SpeechSocket | None) -> None:
        for context in list(self._contexts.values()):
            if on is None or context.socket is on:
                context.lose(fault)

    async def _drop_socket(self) -> None:
        socket, self._socket = self._socket, None
        if socket is not None:
            with suppress(WebSocketException, OSError):
                await socket.close()


def close_code(error: WebSocketException | OSError) -> tuple[int | None, str]:
    if isinstance(error, ConnectionClosed) and error.rcvd is not None:
        return error.rcvd.code, error.rcvd.reason
    return None, ""


class SentenceAudio:
    def __init__(self) -> None:
        self.chunks: asyncio.Queue[bytes | ProviderFault | None] = asyncio.Queue()
        self.words: list[WordTiming] = []
        self.audio_bytes = 0
        self.detached = False

    @property
    def seconds(self) -> float:
        return self.audio_bytes / BYTES_PER_SECOND

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


async def silent(text: str) -> AsyncGenerator[bytes]:
    del text
    return
    yield b""
