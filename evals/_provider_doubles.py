import asyncio
import base64
import codecs
import hashlib
import json
import math
import os
import re
import struct
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import uvicorn
import websockets
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from evals._visual_cassette import ExperimentBudget
from fakeyou.config.settings import CartesiaTTS, GoogleReply, Settings
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.perception.interpreter import INSPECTION_PROMPT_MARKER
from fakeyou.speech.websocket import secure_scheme

ProviderMode = Literal["live", "record", "record_looks", "replay"]
GOOGLE = "https://generativelanguage.googleapis.com"
CASSETTES = Path(__file__).parent / "fixtures" / "simulator" / "cassettes"
MIN_SIMILARITY = 0.75
SAME_CALL = 0.8
LATEST_WORDS = 12
RECENT_WORDS = 40
WORD = re.compile(r"\w+")
FORWARDED_HEADERS = ("content-type", "x-goog-api-key", "x-goog-api-client", "x-server-timeout")
SPOKEN_CHARS_PER_SECOND = 14.0
FIRST_CHUNK_SECONDS = 0.15
CHUNK_SECONDS = 0.1
TONE_HZ = 220.0
TONE_LEVEL = 0.3
UPSTREAM_TIMEOUT_SECONDS = 60.0
SNAPSHOT = CASSETTES / "settings.json"
SECRET_MASK = "**********"
REPLY_ROLES = ("reply", "reply_strong", "reply_fallback", "debrief")
DOUBLE_KEY = "double"
STT_ROUTE = "stt"
RECORDED_LOOK_MARKER = "Answer the question using only what is visible in the image"
CONSERVATIVE_LOOK_USD = 0.01
VOICE_LEVEL = 0.01
QUIET_GAP_SECONDS = 0.6
CLAIM_WINDOW_SECONDS = 3.0
END_GRACE_SECONDS = 3.0
VOICE_END = "simulatorVoiceEnd"
VOLATILE = (
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<id>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ][\d:.]+(?:Z|[+-]\d{2}:?\d{2})?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<id>"),
    (re.compile(r"(seconds_ago\\?\"?\s*[:=]\s*)\d+"), r"\1<n>"),
    (re.compile(r"(_ms\\?\"?\s*[:=]\s*)[\d.]+"), r"\1<n>"),
)


def provider_mode() -> ProviderMode:
    mode = os.environ.get("SIMULATOR_PROVIDERS", "live")
    if mode not in ("live", "record", "record_looks", "replay"):
        raise ValueError(
            f"SIMULATOR_PROVIDERS must be live, record, record_looks or replay, not {mode!r}"
        )
    return cast(ProviderMode, mode)


def _usage_from_body(body: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        whole = json.loads(body)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        payload = whole
    for line in body.splitlines() if not payload else ():
        text = line.strip()
        if text.startswith("data:"):
            text = text[len("data:") :].strip()
        if not text or text == "[DONE]":
            continue
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
    usage = payload.get("usageMetadata") or {}
    return {
        "input_tokens": int(usage.get("promptTokenCount") or 0),
        "output_tokens": int(usage.get("candidatesTokenCount") or 0),
        "total_tokens": int(usage.get("totalTokenCount") or 0),
    }


def _is_look_request(body: bytes) -> bool:
    try:
        request = json.loads(body)
    except ValueError:
        return False
    contents = request.get("contents") or []
    if not contents:
        return False
    return INSPECTION_PROMPT_MARKER in json.dumps(contents[-1], ensure_ascii=False)


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


@dataclass(frozen=True, order=True, slots=True)
class _Score:
    latest: float
    recent: float
    unused: bool
    earlier: int
    whole: float

    def __post_init__(self) -> None:
        for name in ("latest", "recent", "whole"):
            object.__setattr__(self, name, round(getattr(self, name), 2))

    @property
    def index(self) -> int:
        return -self.earlier


def _token_vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in WORD.findall(text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4]) % dimensions
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


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
    heard: set[str] = field(default_factory=set)
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

    def replace_looks(self, route: str, entry: Mapping[str, Any]) -> None:
        kept = [
            item
            for item in self.entries
            if not (
                item.get("route") == route and RECORDED_LOOK_MARKER in str(item.get("focus", ""))
            )
        ]
        kept.append(dict(entry))
        self.entries = kept
        self.path.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept),
            encoding="utf-8",
        )

    def miss(self, detail: Mapping[str, Any]) -> None:
        self.misses.append(json.dumps(detail, ensure_ascii=False)[:400])
        artifacts = os.environ.get("SIMULATOR_ARTIFACT_DIR")
        if artifacts:
            with (Path(artifacts) / "cassette-misses.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps({"cassette": self.path.stem, **detail}) + "\n")

    def speech(self, clip: str) -> list[list[Any]] | None:
        takes: dict[str, list[list[Any]]] = {}
        for entry in self.entries:
            if entry["route"] == STT_ROUTE and entry["clip"] == clip:
                takes.setdefault(entry["take"], []).append([entry["offset"], entry["message"]])
        if not takes:
            return None
        fresh = [take for take in takes if take not in self.heard]
        chosen = fresh[0] if fresh else list(takes)[-1]
        self.heard.add(chosen)
        return takes[chosen]

    def match(self, route: str, key: RequestKey) -> dict[str, Any] | None:
        candidates = [
            index
            for index, entry in enumerate(self.entries)
            if entry["route"] == route and _similarity(key.system, entry["system"]) >= SAME_CALL
        ]
        scored = [
            _Score(
                latest=_similarity(key.focus, self.entries[index]["focus"], tail=LATEST_WORDS),
                recent=_similarity(key.focus, self.entries[index]["focus"], tail=RECENT_WORDS),
                whole=_similarity(key.focus, self.entries[index]["focus"]),
                unused=index not in self.used,
                earlier=-index,
            )
            for index in candidates
        ]
        closest = max(scored, default=None)
        if closest is None or max(closest.latest, closest.whole) < MIN_SIMILARITY:
            self.miss(
                {
                    "route": route,
                    "score": max(closest.latest, closest.whole) if closest else 0.0,
                    "asked": key.focus[-600:],
                    "closest": self.entries[closest.index]["focus"][-600:] if closest else None,
                }
            )
            return None
        if not closest.unused:
            self.reused += 1
        self.used.add(closest.index)
        return self.entries[closest.index]


@dataclass(frozen=True, slots=True)
class Said:
    clip: str
    at: float


@dataclass(slots=True)
class Claim:
    clip: str
    onset: float
    take: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    end: float | None = None
    ended: asyncio.Event = field(default_factory=asyncio.Event)


HeardEvent = tuple[Literal["start", "end"], Claim]


class _Hearing:
    def __init__(self, said: list[Said]) -> None:
        self._said = said
        self._taken: set[int] = set()
        self._loud = False
        self._quiet_since = -math.inf
        self.claim: Claim | None = None

    def hear(self, pcm: bytes) -> HeardEvent | None:
        now = time.monotonic()
        if _level(pcm) < VOICE_LEVEL:
            if self._loud:
                self._loud = False
                self._quiet_since = now
            claim = self.claim
            if (
                claim is not None
                and claim.end is None
                and now - self._quiet_since >= QUIET_GAP_SECONDS
            ):
                claim.end = self._quiet_since
                claim.ended.set()
                return ("end", claim)
            return None
        starting = not self._loud and now - self._quiet_since >= QUIET_GAP_SECONDS
        self._loud = True
        if not starting:
            return None
        recent = [
            index
            for index, said in enumerate(self._said)
            if index not in self._taken and 0 <= now - said.at <= CLAIM_WINDOW_SECONDS
        ]
        if not recent:
            return None
        self._taken.add(recent[-1])
        self.claim = Claim(self._said[recent[-1]].clip, now)
        return ("start", self.claim)


def _level(pcm: bytes) -> float:
    samples = memoryview(pcm[: len(pcm) - len(pcm) % 2]).cast("h")
    if not len(samples):
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768


async def _spoken(socket: WebSocket, claim: Claim, messages: list[list[Any]]) -> None:
    marker = next(
        (offset for offset, message in messages if message.get("type") == VOICE_END), None
    )
    for offset, message in messages:
        if message.get("type") == VOICE_END:
            continue
        due = claim.onset + offset
        if marker is not None and offset >= marker:
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    claim.ended.wait(), max(0.0, due - time.monotonic()) + END_GRACE_SECONDS
                )
            if claim.end is not None:
                due = claim.end + offset - marker
        delay = due - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        with suppress(WebSocketDisconnect, RuntimeError):
            await socket.send_json(message)


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_secrets(item) for key, item in value.items() if item != SECRET_MASK}
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def _require_google(settings: Settings) -> None:
    roles = {name: getattr(settings, name) for name in REPLY_ROLES}
    roles["turn_judge"] = settings.turn_judge.model
    unsupported = [
        name
        for name, role in roles.items()
        if role is not None and not isinstance(role, GoogleReply)
    ]
    if settings.perception.interpreter.provider != "google":
        unsupported.append("perception")
    if unsupported:
        raise ValueError(f"recording supports Google models only, not {unsupported}")


@dataclass(slots=True)
class ProviderDoubles:
    mode: ProviderMode
    cassette: Cassette
    base_url: str = ""
    _client: httpx.AsyncClient | None = None
    _stt_upstream: str = ""
    _said: list["Said"] = field(default_factory=list)

    def settings(self, settings: Settings) -> Settings:
        if self.mode == "live":
            return settings
        if self.mode == "record":
            _require_google(settings)
            snapshot = _without_secrets(settings.model_dump(mode="json", exclude={"paths"}))
            SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
            self._stt_upstream = settings.stt.url
            return self._pointed(settings, None)
        if self.mode == "record_looks":
            snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
            recorded = Settings.model_validate({**snapshot, "paths": settings.paths.model_dump()})
            _require_google(recorded)
            self._stt_upstream = recorded.stt.url
            pointed = self._pointed(recorded, SecretStr(DOUBLE_KEY))
            return pointed.model_copy(
                update={
                    "perception": pointed.perception.model_copy(
                        update={
                            "interpreter": pointed.perception.interpreter.model_copy(
                                update={"api_key": settings.perception.interpreter.api_key}
                            )
                        }
                    )
                }
            )
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        recorded = Settings.model_validate({**snapshot, "paths": settings.paths.model_dump()})
        return self._pointed(recorded, SecretStr(DOUBLE_KEY))

    def _pointed(self, settings: Settings, key: SecretStr | None) -> Settings:
        pointed: dict[str, Any] = {"base_url": self.base_url, **({"api_key": key} if key else {})}

        def google(role: Any) -> Any:
            return role.model_copy(update=pointed) if isinstance(role, GoogleReply) else role

        return settings.model_copy(
            update={
                **{name: google(getattr(settings, name)) for name in REPLY_ROLES},
                "turn_judge": settings.turn_judge.model_copy(
                    update={"model": google(settings.turn_judge.model)}
                ),
                "perception": settings.perception.model_copy(
                    update={
                        "interpreter": settings.perception.interpreter.model_copy(update=pointed)
                    }
                ),
                "tts": CartesiaTTS(api_key=SecretStr(DOUBLE_KEY), base_url=self.base_url),
                "tts_fallback": None,
                "memory": settings.memory.model_copy(
                    update={
                        "embedder": settings.memory.embedder.model_copy(
                            update={
                                "base_url": self.base_url + "/v1",
                                "api_key": SecretStr(DOUBLE_KEY),
                            }
                        )
                    }
                ),
                "stt": settings.stt.model_copy(
                    update={
                        "url": secure_scheme(self.base_url) + "/stt",
                        **({"api_key": key} if key else {}),
                    }
                ),
            }
        )

    def script(self, clip: str) -> None:
        self._said.append(Said(clip, time.monotonic()))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                Route("/v1beta/models/{call:path}", self._gemini, methods=["POST"]),
                WebSocketRoute("/tts/websocket", self._cartesia),
                WebSocketRoute("/stt", self._muse),
            ]
        )

    async def _embeddings(self, request: Request) -> Response:
        payload = await request.json()
        inputs = payload.get("input") or []
        if isinstance(inputs, str):
            inputs = [inputs]
        dimensions = int(payload.get("dimensions") or 1024)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": _token_vector(str(text), dimensions),
                    }
                    for index, text in enumerate(inputs)
                ],
                "model": payload.get("model", "double"),
            }
        )

    async def _gemini(self, request: Request) -> Response:
        body = await request.body()
        route = request.url.path
        key = RequestKey.of(body)
        if self.mode == "record" or (self.mode == "record_looks" and _is_look_request(body)):
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
        guard = ExperimentBudget()
        look = _is_look_request(body)
        if self.mode == "record_looks" and not guard.allows(CONSERVATIVE_LOOK_USD):
            return JSONResponse(
                {"error": {"code": 429, "message": "experiment budget reached"}}, status_code=429
            )
        headers = {
            name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers
        }
        started = time.monotonic()
        attempt_id = guard.reserve(
            {"source": "simulator", "route": route, "focus": key.focus[-200:]}
        )
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
                recorded = "".join(text for _delay, text in chunks)
                entry = {
                    "route": route,
                    "system": key.system,
                    "focus": key.focus,
                    "status": upstream.status_code,
                    "media_type": media_type,
                    "chunks": chunks,
                }
                if self.mode == "record_looks" and look:
                    cassette.replace_looks(route, entry)
                else:
                    cassette.add(entry)
                guard.settle(
                    attempt_id,
                    usage=_usage_from_body(recorded),
                    inference=200 <= upstream.status_code < 300,
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

    async def _muse(self, socket: WebSocket) -> None:
        await socket.accept()
        handshake = await socket.receive_text()
        hearing = _Hearing(self._said)
        if self.mode == "record":
            await self._relay_muse(socket, handshake, hearing)
        else:
            await self._replay_muse(socket, hearing)

    def _keep_heard(self, claim: Claim | None, message: Any, *, at: float | None = None) -> None:
        if claim is None:
            return
        self.cassette.add(
            {
                "route": STT_ROUTE,
                "clip": claim.clip,
                "take": claim.take,
                "offset": round((at or time.monotonic()) - claim.onset, 3),
                "message": message,
            }
        )

    async def _relay_muse(self, socket: WebSocket, handshake: str, hearing: "_Hearing") -> None:
        query = socket.url.query
        async with websockets.connect(f"{self._stt_upstream}?{query}") as upstream:
            await upstream.send(handshake)
            await socket.send_text(str(await upstream.recv()))

            async def answers() -> None:
                async for raw in upstream:
                    if isinstance(raw, str):
                        self._keep_heard(hearing.claim, json.loads(raw))
                        await socket.send_text(raw)

            answering = asyncio.ensure_future(answers())
            try:
                while (message := await socket.receive())["type"] != "websocket.disconnect":
                    if audio := message.get("bytes"):
                        heard = hearing.hear(audio)
                        if heard is not None and heard[0] == "end":
                            self._keep_heard(heard[1], {"type": VOICE_END}, at=heard[1].end)
                        await upstream.send(audio)
                    elif text := message.get("text"):
                        await upstream.send(text)
            finally:
                answering.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await answering

    async def _replay_muse(self, socket: WebSocket, hearing: "_Hearing") -> None:
        await socket.send_json({"sessionId": f"double-{id(hearing)}"})
        speaking: list[asyncio.Task[None]] = []
        try:
            while (message := await socket.receive())["type"] != "websocket.disconnect":
                audio = message.get("bytes")
                heard = hearing.hear(audio) if audio else None
                if heard is None or heard[0] != "start":
                    continue
                claim = heard[1]
                recorded = self.cassette.speech(claim.clip)
                if recorded is None:
                    self.cassette.miss({"route": STT_ROUTE, "clip": claim.clip})
                    continue
                speaking.append(asyncio.ensure_future(_spoken(socket, claim, recorded)))
        finally:
            for task in speaking:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*speaking, return_exceptions=True)

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
