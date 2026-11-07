import asyncio
import base64
import codecs
import json
import math
import os
import re
import struct
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from fakeyou.config.settings import CartesiaTTS, GoogleReply, Settings
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE

ProviderMode = Literal["live", "record", "replay"]
GOOGLE = "https://generativelanguage.googleapis.com"
CASSETTES = Path(__file__).parent / "fixtures" / "simulator" / "cassettes"
MIN_SIMILARITY = 0.75
SAME_CALL = 0.8
TIE = 0.02
LATEST_WORDS = 40
WORD = re.compile(r"\w+")
FORWARDED_HEADERS = ("content-type", "x-goog-api-key", "x-goog-api-client", "x-server-timeout")
SPOKEN_CHARS_PER_SECOND = 14.0
FIRST_CHUNK_SECONDS = 0.15
CHUNK_SECONDS = 0.1
TONE_HZ = 220.0
TONE_LEVEL = 0.3
UPSTREAM_TIMEOUT_SECONDS = 60.0
VOLATILE = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<id>"),
    (re.compile(r"(seconds_ago\\?\"?\s*[:=]\s*)\d+"), r"\1<n>"),
    (re.compile(r"(_ms\\?\"?\s*[:=]\s*)[\d.]+"), r"\1<n>"),
)


def provider_mode() -> ProviderMode:
    mode = os.environ.get("SIMULATOR_PROVIDERS", "live")
    if mode not in ("live", "record", "replay"):
        raise ValueError(f"SIMULATOR_PROVIDERS must be live, record or replay, not {mode!r}")
    return cast(ProviderMode, mode)


def _without_images(value: Any) -> Any:
    if isinstance(value, dict):
        if "inlineData" in value:
            return {**value, "inlineData": "<image>"}
        return {key: _without_images(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_without_images(item) for item in value]
    return value


def _stable(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False)
    for pattern, placeholder in VOLATILE:
        text = pattern.sub(placeholder, text)
    return text


@dataclass(frozen=True, slots=True)
class RequestKey:
    system: str
    focus: str

    @classmethod
    def of(cls, body: bytes) -> "RequestKey":
        try:
            request = _without_images(json.loads(body))
        except ValueError:
            return cls("", _stable(body.decode("utf-8", errors="replace")))
        contents = request.get("contents") or [None]
        return cls(_stable(request.get("systemInstruction")), _stable(contents[-1]))


def _similarity(left: str, right: str, *, tail: int | None = None) -> float:
    if left == right:
        return 1.0
    words = WORD.findall(left.casefold())
    others = WORD.findall(right.casefold())
    if tail:
        words, others = words[-tail:], others[-tail:]
    return SequenceMatcher(None, words, others, autojunk=False).ratio()


@dataclass(slots=True)
class Cassette:
    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)
    used: set[int] = field(default_factory=set)
    misses: list[str] = field(default_factory=list)
    reused: int = 0

    @classmethod
    def load(cls, path: Path) -> "Cassette":
        if not path.exists():
            return cls(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        return cls(path, [json.loads(line) for line in lines if line])

    @classmethod
    def fresh(cls, path: Path) -> "Cassette":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return cls(path)

    def add(self, entry: Mapping[str, Any]) -> None:
        self.entries.append(dict(entry))
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def match(self, route: str, key: RequestKey) -> dict[str, Any] | None:
        scored = [
            (
                _similarity(key.focus, entry["focus"], tail=LATEST_WORDS),
                _similarity(key.focus, entry["focus"]),
                index,
            )
            for index, entry in enumerate(self.entries)
            if entry["route"] == route and _similarity(key.system, entry["system"]) >= SAME_CALL
        ]
        best = max((latest for latest, _, _ in scored), default=0.0)
        if best < MIN_SIMILARITY:
            self.misses.append(f"{route} ({best:.2f}): {key.focus[-200:]}")
            return None
        near = sorted(
            (index not in self.used, round(whole, 2), -index)
            for latest, whole, index in scored
            if best - latest <= TIE
        )
        unused, _, negative_index = near[-1]
        if not unused:
            self.reused += 1
        self.used.add(-negative_index)
        return self.entries[-negative_index]


@dataclass(slots=True)
class ProviderDoubles:
    mode: ProviderMode
    cassette: Cassette
    base_url: str = ""
    _client: httpx.AsyncClient | None = None

    def settings(self, settings: Settings) -> Settings:
        if self.mode == "live":
            return settings
        update: dict[str, Any] = {
            name: role.model_copy(update={"base_url": self.base_url})
            for name in ("reply", "reply_strong", "reply_fallback", "debrief")
            if isinstance(role := getattr(settings, name), GoogleReply)
        }
        judge = settings.turn_judge.model
        if isinstance(judge, GoogleReply):
            update["turn_judge"] = settings.turn_judge.model_copy(
                update={"model": judge.model_copy(update={"base_url": self.base_url})}
            )
        interpreter = settings.perception.interpreter
        if interpreter.provider == "google":
            update["perception"] = settings.perception.model_copy(
                update={"interpreter": interpreter.model_copy(update={"base_url": self.base_url})}
            )
        update["tts"] = CartesiaTTS(api_key=SecretStr("double"), base_url=self.base_url)
        update["tts_fallback"] = None
        return settings.model_copy(update=update)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1beta/models/{call:path}", self._gemini, methods=["POST"]),
                WebSocketRoute("/tts/websocket", self._cartesia),
            ]
        )

    async def _gemini(self, request: Request) -> Response:
        body = await request.body()
        route = request.url.path
        key = RequestKey.of(body)
        if self.mode == "record":
            return await self._record(request, route, body, key)
        entry = self.cassette.match(route, key)
        if entry is None:
            return JSONResponse(
                {"error": {"code": 500, "message": "cassette miss", "status": "INTERNAL"}},
                status_code=500,
            )
        return StreamingResponse(
            _replayed(entry["chunks"]),
            status_code=entry["status"],
            media_type=entry["media_type"],
        )

    async def _record(self, request: Request, route: str, body: bytes, key: RequestKey) -> Response:
        assert self._client is not None
        headers = {
            name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers
        }
        started = time.monotonic()
        upstream = await self._client.send(
            self._client.build_request(
                "POST", GOOGLE + route, params=request.url.query, content=body, headers=headers
            ),
            stream=True,
        )
        cassette = self.cassette
        media_type = upstream.headers.get("content-type", "application/json")

        async def relay() -> AsyncIterator[bytes]:
            decoder = codecs.getincrementaldecoder("utf-8")()
            chunks: list[list[Any]] = []
            last = started
            try:
                async for chunk in upstream.aiter_bytes():
                    now = time.monotonic()
                    chunks.append([round(now - last, 4), decoder.decode(chunk)])
                    last = now
                    yield chunk
            finally:
                await upstream.aclose()
                cassette.add(
                    {
                        "route": route,
                        "system": key.system,
                        "focus": key.focus,
                        "status": upstream.status_code,
                        "media_type": media_type,
                        "chunks": chunks,
                    }
                )

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type=media_type)

    async def _cartesia(self, socket: WebSocket) -> None:
        await socket.accept()
        speaker = _SyntheticSpeaker(socket)
        try:
            while True:
                speaker.receive(await socket.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            await speaker.close()

    @asynccontextmanager
    async def running(self) -> AsyncIterator["ProviderDoubles"]:
        if self.mode == "live":
            yield self
            return
        server = uvicorn.Server(
            uvicorn.Config(
                self.app(), host="127.0.0.1", port=0, log_level="warning", ws="websockets"
            )
        )
        serving = asyncio.ensure_future(server.serve())
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS) as client:
            self._client = client
            try:
                while not server.started:
                    if serving.done():
                        serving.result()
                    await asyncio.sleep(0.05)
                port = server.servers[0].sockets[0].getsockname()[1]
                self.base_url = f"http://127.0.0.1:{port}"
                yield self
            finally:
                server.should_exit = True
                with suppress(asyncio.CancelledError):
                    await serving


def provider_doubles(scenario: str) -> ProviderDoubles:
    mode = provider_mode()
    folder = Path(os.environ.get("SIMULATOR_CASSETTE_DIR", CASSETTES))
    path = folder / f"{scenario}.jsonl"
    cassette = Cassette.fresh(path) if mode == "record" else Cassette.load(path)
    return ProviderDoubles(mode, cassette)


async def _replayed(chunks: list[list[Any]]) -> AsyncIterator[bytes]:
    for delay, text in chunks:
        await asyncio.sleep(delay)
        yield text.encode("utf-8")


def _tone(seconds: float) -> bytes:
    frames = int(seconds * PLAYBACK_SAMPLE_RATE)
    peak = TONE_LEVEL * 32767
    return struct.pack(
        f"<{frames}h",
        *(
            int(peak * math.sin(2 * math.pi * TONE_HZ * frame / PLAYBACK_SAMPLE_RATE))
            for frame in range(frames)
        ),
    )


TONE_CHUNK = base64.b64encode(_tone(CHUNK_SECONDS)).decode()


class _SyntheticSpeaker:
    def __init__(self, socket: WebSocket) -> None:
        self._socket = socket
        self._sending = asyncio.Lock()
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._cancelled: set[str] = set()

    def receive(self, message: dict[str, Any]) -> None:
        context_id = str(message.get("context_id", ""))
        if message.get("cancel"):
            self._cancelled.add(context_id)
            return
        if context_id not in self._queues:
            self._queues[context_id] = asyncio.Queue()
            self._workers.append(asyncio.ensure_future(self._speak(context_id)))
        self._queues[context_id].put_nowait(message)

    async def _speak(self, context_id: str) -> None:
        queue = self._queues[context_id]
        flushes = 0
        while context_id not in self._cancelled:
            message = await queue.get()
            text = str(message.get("transcript", ""))
            if text:
                flushes += 1
                await self._sentence(context_id, flushes, text)
            if not message.get("continue", True):
                await self._send({"type": "done", "done": True, "context_id": context_id})
                return

    async def _sentence(self, context_id: str, flush_id: int, text: str) -> None:
        await asyncio.sleep(FIRST_CHUNK_SECONDS)
        chunks = max(1, round(len(text) / SPOKEN_CHARS_PER_SECOND / CHUNK_SECONDS))
        for _ in range(chunks):
            if context_id in self._cancelled:
                return
            await self._send(
                {
                    "type": "chunk",
                    "data": TONE_CHUNK,
                    "context_id": context_id,
                    "flush_id": flush_id,
                    "done": False,
                }
            )
            await asyncio.sleep(CHUNK_SECONDS / 4)
        await self._send(
            {"type": "flush_done", "context_id": context_id, "flush_id": flush_id, "done": False}
        )

    async def _send(self, message: dict[str, Any]) -> None:
        async with self._sending:
            with suppress(WebSocketDisconnect, RuntimeError):
                await self._socket.send_json(message)

    async def close(self) -> None:
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with suppress(asyncio.CancelledError):
                await worker
