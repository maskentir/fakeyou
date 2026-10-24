import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import Any, Protocol, get_args

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import WebSocketException
from websockets.protocol import State

from fakeyou.config.settings import DEFAULTS, STTMode
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES
from fakeyou.foundation.transcript import TranscriptUpdate
from fakeyou.observability.faults import ConsumerSink, FaultReporter, guard_consumer
from fakeyou.observability.journal import Journal
from fakeyou.transcription.contract import (
    SpeechSinks,
    SttConnectionLost,
    TranscriptionError,
    TranscriptSink,
)

MUSE_MAX_BACKLOG_SECONDS = 4
MUSE_MAX_SESSION_SECONDS = 3600.0
OVERLOAD_MARKERS = ("slower than real-time", "backlog too large", "slower than realtime")
RECOVERABLE_ERROR_MARKERS = (*OVERLOAD_MARKERS, "max session duration")

KEEPALIVE_IDLE_SECONDS = 0.5
KEEPALIVE_CHUNK_SECONDS = 0.1
HANDSHAKE_TIMEOUT_SECONDS = 10.0
MUSE_LANGUAGE_NAMES = {"en": "English", "id": "Indonesian"}


class MuseSocket(Protocol):
    @property
    def alive(self) -> bool: ...

    async def send(self, message: str | bytes) -> None: ...

    async def recv(self) -> str | bytes: ...

    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def close(self) -> None: ...


SocketOpener = Callable[[str], Awaitable[MuseSocket]]


class _LiveSocket:
    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    @property
    def alive(self) -> bool:
        return self._connection.state is State.OPEN

    async def send(self, message: str | bytes) -> None:
        try:
            await self._connection.send(message)
        except (WebSocketException, OSError) as error:
            raise SttConnectionLost("Muse socket send failed") from error

    async def recv(self) -> str | bytes:
        try:
            return await self._connection.recv()
        except (WebSocketException, OSError) as error:
            raise SttConnectionLost("Muse socket receive failed") from error

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        return self._connection.__aiter__()

    async def close(self) -> None:
        try:
            await self._connection.close()
        except (WebSocketException, OSError) as error:
            raise SttConnectionLost("Muse socket close failed") from error


async def connect_muse_socket(url: str) -> MuseSocket:
    try:
        connection = await websockets.connect(url, open_timeout=HANDSHAKE_TIMEOUT_SECONDS)
    except (WebSocketException, OSError, TimeoutError) as error:
        raise TranscriptionError("Muse connection failed") from error
    return _LiveSocket(connection)


class KeywordSource:
    def __init__(self, terms: Sequence[str] = ()) -> None:
        self._base = tuple(dict.fromkeys(term for term in terms if term))
        self._context: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self._base, *self._context)))

    def update(self, terms: Sequence[str]) -> bool:
        merged = tuple(dict.fromkeys(term for term in terms if term))
        if merged == self._context:
            return False
        self._context = merged
        return True


@dataclass(frozen=True, slots=True)
class MuseOptions:
    model: str = DEFAULTS.stt.model
    mode: STTMode = DEFAULTS.stt.mode
    url: str = DEFAULTS.stt.url
    language: str = DEFAULTS.speech.language
    keywords: KeywordSource = field(default_factory=KeywordSource)
    sample_rate: int = CAPTURE_SAMPLE_RATE
    handshake_timeout_seconds: float = HANDSHAKE_TIMEOUT_SECONDS
    keepalive_idle_seconds: float = KEEPALIVE_IDLE_SECONDS
    keepalive_chunk_seconds: float = KEEPALIVE_CHUNK_SECONDS


@dataclass(frozen=True, slots=True)
class MuseDeps:
    journal: Journal | None = None
    faults: FaultReporter | None = None
    open_socket: SocketOpener = connect_muse_socket
    clock: Callable[[], float] = time.monotonic


@dataclass(slots=True)
class _Turn:
    turn_id: Any
    labels: list[str] = field(default_factory=list)
    interims: int = 0
    first_interim_at: float | None = None


class MuseClient:
    def __init__(
        self,
        *,
        api_key: str,
        options: MuseOptions,
        sinks: SpeechSinks,
        deps: MuseDeps | None = None,
    ) -> None:
        if options.language not in MUSE_LANGUAGE_NAMES:
            raise ValueError(f"unsupported Muse language: {options.language}")
        if options.mode not in get_args(STTMode):
            raise ValueError(f"unsupported Muse mode: {options.mode}")
        resolved = deps or MuseDeps()
        self._api_key = api_key
        self._options = options
        self._on_update = sinks.on_update
        self._on_interim = sinks.on_interim
        self._on_lost = sinks.on_lost
        self._journal = resolved.journal
        self._faults = resolved.faults
        self._open_socket = resolved.open_socket
        self._clock = resolved.clock
        self._bytes_per_second = options.sample_rate * PCM16_SAMPLE_BYTES
        self._outbox: deque[bytes] = deque()
        self._outbox_bytes = 0
        self._wake = asyncio.Event()
        self._session_ready = asyncio.Event()
        self._socket: MuseSocket | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._turn: _Turn | None = None
        self._sessions = 0
        self._last_audio_at: float | None = None
        self._stream_started: float | None = None
        self._sent_bytes = 0
        self._keepalive_burst_bytes = self._bytes_per_second
        self._keepalive_active = False
        self._lost = False
        self._closed = False

    @property
    def ready(self) -> bool:
        return self._socket_ready()

    @property
    def session_ready(self) -> asyncio.Event:
        return self._session_ready

    @property
    def socket_dead(self) -> bool:
        return self._lost

    @property
    def buffered_seconds(self) -> float:
        return self._outbox_bytes / self._bytes_per_second

    def idle_seconds(self, now: float) -> float | None:
        if self._last_audio_at is None:
            return None
        return max(now - self._last_audio_at, 0.0)

    def session_expires_in(self, now: float) -> float | None:
        if self._stream_started is None or not self._socket_ready():
            return None
        return MUSE_MAX_SESSION_SECONDS - (now - self._stream_started)

    async def start(self) -> None:
        if self._closed:
            raise TranscriptionError("Muse client is closed")
        if self._socket_ready():
            return
        socket = await self._open_session()
        self._socket = socket
        self._lost = False
        self._sessions += 1
        self._stream_started = self._clock()
        self._sent_bytes = 0
        if self._last_audio_at is None:
            self._last_audio_at = self._clock()
        self._session_ready.set()
        self._tasks = [
            asyncio.create_task(self._receive(socket)),
            asyncio.create_task(self._send_loop()),
        ]

    def update_keywords(self, terms: Sequence[str]) -> bool:
        return self._options.keywords.update(terms)

    async def reconnect_when_idle(self) -> None:
        await self._drop_socket()
        try:
            await self.start()
        except BaseException:
            self._lost = True
            raise

    async def close(self) -> None:
        self._closed = True
        await self._drop_socket()

    def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._last_audio_at = self._clock()
        self._keepalive_active = False
        self._outbox.append(pcm)
        self._outbox_bytes += len(pcm)
        self._trim_backlog()
        self._wake.set()

    async def _open_session(self) -> MuseSocket:
        url = f"{self._options.url}?sessionId=stream-{uuid.uuid4()}"
        socket = await self._open_socket(url)
        try:
            await socket.send(json.dumps(self._handshake()))
            ack = await asyncio.wait_for(socket.recv(), self._options.handshake_timeout_seconds)
        except TimeoutError as error:
            await _close_quietly(socket)
            raise TranscriptionError("Muse handshake timed out") from error
        except TranscriptionError:
            await _close_quietly(socket)
            raise
        reply = _decode(ack)
        if not isinstance(reply, dict) or "sessionId" not in reply:
            await _close_quietly(socket)
            detail = reply.get("message", reply) if isinstance(reply, dict) else reply
            raise TranscriptionError(f"Muse rejected the session: {detail}")
        return socket

    def _handshake(self) -> dict[str, Any]:
        options = self._options
        handshake: dict[str, Any] = {
            "authorization": {"accessToken": f"Bearer {self._api_key}"},
            "audioEncoding": f"PCM_{options.sample_rate // 1000}KHZ",
            "model": options.model,
            "mode": options.mode,
            "partialMode": "CUMULATIVE",
            "emitAudioProgress": False,
            "languageBias": [MUSE_LANGUAGE_NAMES[options.language]],
        }
        if options.keywords.terms:
            handshake["keywords"] = list(options.keywords.terms)
        return handshake

    def _socket_ready(self) -> bool:
        socket = self._socket
        return socket is not None and socket.alive and self._session_ready.is_set()

    def _trim_backlog(self) -> None:
        limit = int(self._bytes_per_second * MUSE_MAX_BACKLOG_SECONDS)
        dropped = 0
        while self._outbox_bytes > limit and len(self._outbox) > 1:
            chunk = self._outbox.popleft()
            self._outbox_bytes -= len(chunk)
            dropped += len(chunk)
        if dropped and self._journal is not None:
            self._journal.record(
                "stt_audio_dropped",
                source="stt",
                dropped_ms=round(dropped * 1000 / self._bytes_per_second, 1),
                reason="backlog_limit",
            )

    async def _send_loop(self) -> None:
        options = self._options
        while True:
            if self._outbox and self._socket_ready():
                await self._send_next()
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), options.keepalive_chunk_seconds)
            except TimeoutError:
                pass
            self._wake.clear()
            if self._socket_ready() and not self._outbox and self._keepalive_due():
                await self._send_keepalive()

    async def _send_next(self) -> None:
        socket = self._socket
        if socket is None:
            return
        chunk = self._outbox.popleft()
        self._outbox_bytes -= len(chunk)
        try:
            await socket.send(chunk)
        except TranscriptionError as error:
            self._lose(error)
            return
        self._sent_bytes += len(chunk)

    def _keepalive_due(self) -> bool:
        if self._last_audio_at is None:
            return False
        idle = self._clock() - self._last_audio_at
        if idle < self._options.keepalive_idle_seconds:
            return False
        if not self._keepalive_active:
            self._keepalive_active = True
            if self._journal is not None:
                self._journal.record(
                    "stt_keepalive_started", source="stt", idle_ms=round(idle * 1000, 1)
                )
        return True

    async def _send_keepalive(self) -> None:
        socket = self._socket
        if socket is None or self._stream_started is None:
            return
        elapsed_bytes = int((self._clock() - self._stream_started) * self._bytes_per_second)
        behind = elapsed_bytes - self._sent_bytes
        behind -= behind % PCM16_SAMPLE_BYTES
        if behind <= 0:
            return
        chunk = min(behind, self._keepalive_burst_bytes)
        try:
            await socket.send(bytes(chunk))
        except TranscriptionError as error:
            self._lose(error)
            return
        self._sent_bytes += chunk

    async def _receive(self, socket: MuseSocket) -> None:
        try:
            async for raw in socket:
                if isinstance(raw, bytes):
                    continue
                await self._handle(raw)
        except TranscriptionError as error:
            if socket is self._socket:
                self._lose(error)
            return
        except (WebSocketException, OSError):
            if socket is self._socket:
                self._lose(SttConnectionLost("Muse socket closed unexpectedly"))
            return
        if socket is self._socket and not self._closed:
            self._lose(SttConnectionLost("Muse closed the session"))

    def _lose(self, error: TranscriptionError) -> None:
        if self._lost or self._closed:
            return
        self._lost = True
        self._session_ready.clear()
        self._socket = None
        for task in self._tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self._tasks = []
        if self._journal is not None:
            self._journal.record("stt_socket_failed", source="stt", reason=str(error))
        if self._on_lost is not None:
            self._on_lost(error)

    async def _drop_socket(self) -> None:
        socket, self._socket = self._socket, None
        self._session_ready.clear()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if socket is not None:
            if socket.alive:
                try:
                    await socket.send(json.dumps({"type": "endStream"}))
                except TranscriptionError:
                    pass
            await _close_quietly(socket)
        self._turn = None

    async def _handle(self, raw: str) -> None:
        message = _decode(raw)
        if not isinstance(message, dict):
            self._note("stt_message_dropped")
            return
        kind = message.get("type")
        if kind == "speechStart":
            self._turn = _Turn(turn_id=message.get("turnId"))
        elif kind == "transcript":
            await self._on_transcript(message)
        elif kind == "speaker":
            self._attach_label(message.get("label"))
        elif kind == "speechComplete":
            await self._complete_turn(message)
        elif kind == "error":
            if _is_recoverable(message.get("message", "unknown")):
                raise SttConnectionLost(f"Muse error: {message.get('message', '')}")
            raise TranscriptionError(f"Muse error: {message.get('message', 'unknown')}")
        elif kind in ("speechEnd", "audioProgress"):
            return
        else:
            self._note("stt_message_skipped", message_type=str(kind))

    async def _on_transcript(self, message: dict[str, Any]) -> None:
        text = str(message.get("transcript") or "").strip()
        if not text:
            return
        if message.get("final"):
            self._note("stt_final_observed")
            await self._emit_final(text, ())
            return
        turn = self._turn
        if turn is not None:
            if turn.first_interim_at is None:
                turn.first_interim_at = self._clock()
            turn.interims += 1
        if self._on_interim is not None:
            await self._deliver(
                self._on_interim, TranscriptUpdate(text=text, final=False, timestamp=_now())
            )

    def _attach_label(self, label: Any) -> None:
        if not isinstance(label, str) or not label:
            return
        turn = self._turn
        if turn is None:
            self._note("stt_speaker_dropped", label=label)
            return
        if label not in turn.labels:
            turn.labels.append(label)

    async def _complete_turn(self, message: dict[str, Any]) -> None:
        turn, self._turn = self._turn, None
        labels: tuple[str, ...] = tuple(turn.labels) if turn is not None else ()
        text = str(message.get("transcript") or "").strip()
        if turn is not None:
            self._record_turn(turn)
        if not text:
            self._note("stt_turn_skipped")
            return
        await self._emit_final(text, self._scoped(labels))

    def _scoped(self, labels: tuple[str, ...]) -> tuple[str, ...]:
        if self._sessions <= 1:
            return labels
        return tuple(f"{label}@{self._sessions}" for label in labels)

    def _record_turn(self, turn: _Turn) -> None:
        if self._journal is None:
            return
        first = turn.first_interim_at
        self._journal.record(
            "stt_turn_recorded",
            source="stt",
            interims=turn.interims,
            first_interim_to_final_ms=(
                round((self._clock() - first) * 1000.0, 1) if first is not None else None
            ),
        )

    async def _emit_final(self, text: str, voice_clusters: tuple[str, ...]) -> None:
        await self._deliver(
            self._on_update,
            TranscriptUpdate(
                text=text,
                final=True,
                timestamp=_now(),
                language=self._options.language,
                voice_clusters=voice_clusters,
            ),
        )

    async def _deliver(self, sink: TranscriptSink, update: TranscriptUpdate) -> None:
        await guard_consumer(
            partial(sink, update),
            sink=ConsumerSink(self._faults, self._journal),
            source="stt",
            detail="transcript_consumer",
        )

    def _note(self, event: str, **fields: Any) -> None:
        if self._journal is not None:
            self._journal.record(event, source="stt", **fields)


def _is_recoverable(message: object) -> bool:
    if not isinstance(message, str):
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in RECOVERABLE_ERROR_MARKERS)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(raw: str | bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def _close_quietly(socket: MuseSocket) -> None:
    try:
        await socket.close()
    except TranscriptionError:
        pass
