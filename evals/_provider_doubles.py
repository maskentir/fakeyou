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
from collections import Counter
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
import uvicorn
import websockets
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from evals._recording_budget import RecordingBudget, RecordingStopped
from evals._replay_cassette import (
    CassetteError,
    HeardTranscripts,
    ModelExchange,
    ReplayLedger,
    ReplayMiss,
    SpeechMessage,
    TurnJudgeShape,
    canonical_request,
    judge_route,
    model_exchange,
    read_cassette,
)
from evals._speech_recording import (
    MUSE_ENDPOINT,
    MUSE_MODEL,
    SessionRefused,
    SpeechRecording,
    SpeechSession,
    SpeechStopped,
)
from fakeyou.config.settings import CartesiaTTS, GoogleReply, Settings
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.speech.websocket import secure_scheme

ProviderMode = Literal["live", "record", "record_missing", "replay"]
GOOGLE = "https://generativelanguage.googleapis.com"
CASSETTES = Path(__file__).parent / "fixtures" / "simulator" / "cassettes"
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
VOICE_LEVEL = 0.01
QUIET_GAP_SECONDS = 0.6
CLAIM_WINDOW_SECONDS = 3.0
END_GRACE_SECONDS = 3.0
VOICE_END = "simulatorVoiceEnd"
SERVER_START_SECONDS = 30.0
MISS_STATUS = 400
RECORDING_BUDGET_ENV = "SIMULATOR_RECORDING_BUDGET"
RECORD_SPEECH_ENV = "SIMULATOR_RECORD_SPEECH"
RECORD_SPEECH_SESSIONS_ENV = "SIMULATOR_RECORD_SPEECH_SESSIONS"
UNSERVED_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MODES: tuple[ProviderMode, ...] = get_args(ProviderMode)


def provider_mode() -> ProviderMode:
    mode = os.environ.get("SIMULATOR_PROVIDERS", "replay")
    if mode not in MODES:
        raise ValueError(f"SIMULATOR_PROVIDERS must be one of {MODES}, not {mode!r}")
    return mode


def _token_vector(text: str, dimensions: int) -> list[float]:
    vector = [0.0] * dimensions
    for token in WORD.findall(text.lower()):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4]) % dimensions
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


@dataclass(frozen=True, slots=True)
class Said:
    clip: str
    at: float
    take: int


@dataclass(slots=True)
class Claim:
    clip: str
    onset: float
    take: int
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
        said = self._said[recent[-1]]
        self.claim = Claim(said.clip, now, said.take)
        return ("start", self.claim)


def _level(pcm: bytes) -> float:
    samples = memoryview(pcm[: len(pcm) - len(pcm) % 2]).cast("h")
    if not len(samples):
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples)) / 32768


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_secrets(item) for key, item in value.items() if item != SECRET_MASK}
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def _with_google_keys(recorded: Settings, live: Settings) -> Settings:
    def keyed(role: Any, source: Any) -> Any:
        if not isinstance(role, GoogleReply) or not isinstance(source, GoogleReply):
            return role
        return role.model_copy(update={"api_key": source.api_key})

    return recorded.model_copy(
        update={
            **{name: keyed(getattr(recorded, name), getattr(live, name)) for name in REPLY_ROLES},
            "turn_judge": recorded.turn_judge.model_copy(
                update={"model": keyed(recorded.turn_judge.model, live.turn_judge.model)}
            ),
            "perception": recorded.perception.model_copy(
                update={
                    "interpreter": recorded.perception.interpreter.model_copy(
                        update={"api_key": live.perception.interpreter.api_key}
                    )
                }
            ),
        }
    )


def _require_google_keys(settings: Settings) -> None:
    roles = {name: getattr(settings, name) for name in REPLY_ROLES}
    roles["turn_judge"] = settings.turn_judge.model
    missing = [
        name
        for name, role in roles.items()
        if isinstance(role, GoogleReply) and role.api_key is None
    ]
    if settings.perception.interpreter.api_key is None:
        missing.append("perception")
    if missing:
        raise ValueError(f"record_missing needs Gemini keys in .env for {missing}")


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


class CassetteWriter:
    def __init__(self, path: Path, *, fresh: bool) -> None:
        self.path = path
        self.appended: list[ModelExchange | SpeechMessage] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            path.write_text("", encoding="utf-8")

    def append(self, line: ModelExchange | SpeechMessage) -> None:
        self.appended.append(line)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line.model_dump_json() + "\n")

    def rewrite(self, lines: Sequence[ModelExchange | SpeechMessage]) -> None:
        self.path.write_text(
            "".join(line.model_dump_json() + "\n" for line in lines), encoding="utf-8"
        )


@dataclass(slots=True)
class ProviderDoubles:
    mode: ProviderMode
    cassette: Path
    ledger: ReplayLedger | None = None
    writer: CassetteWriter | None = None
    base_url: str = ""
    _client: httpx.AsyncClient | None = None
    _stt_upstream: str = ""
    _said: list[Said] = field(default_factory=list)
    _spoken_takes: Counter[str] = field(default_factory=Counter)
    unheard_voice_ends: list[str] = field(default_factory=list)
    transcripts: HeardTranscripts = field(default_factory=HeardTranscripts)
    refused_recordings: list[str] = field(default_factory=list)
    budget: RecordingBudget | None = None
    speech: SpeechRecording | None = None
    stt_upstream_url: str = MUSE_ENDPOINT
    _stt_key: SecretStr | None = None
    _speech_messages: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)

    def settings(self, settings: Settings) -> Settings:
        if self.mode == "live":
            return settings
        if self.mode == "record":
            _require_google(settings)
            snapshot = _without_secrets(settings.model_dump(mode="json", exclude={"paths"}))
            SNAPSHOT.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
            self._stt_upstream = settings.stt.url
            return self._pointed(settings, None)
        snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        recorded = Settings.model_validate({**snapshot, "paths": settings.paths.model_dump()})
        _require_google(recorded)
        if self.mode == "replay":
            return self._pointed(recorded, SecretStr(DOUBLE_KEY))
        keyed = _with_google_keys(recorded, settings)
        _require_google_keys(keyed)
        if self.speech is not None:
            self._stt_key = _speech_key(recorded, settings)
        return self._pointed(keyed, None)

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
                        "llm": settings.memory.llm.model_copy(
                            update={
                                "base_url": self.base_url + "/memory",
                                "api_key": SecretStr(DOUBLE_KEY),
                            }
                        ),
                        "embedder": settings.memory.embedder.model_copy(
                            update={
                                "base_url": self.base_url + "/v1",
                                "api_key": SecretStr(DOUBLE_KEY),
                            }
                        ),
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
        self._spoken_takes[clip] += 1
        self._said.append(Said(clip, time.monotonic(), self._spoken_takes[clip]))

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                Route("/v1beta/models/{call:path}", self._gemini, methods=["POST"]),
                WebSocketRoute("/tts/websocket", self._cartesia),
                WebSocketRoute("/stt", self._muse),
                Route("/{path:path}", self._unserved, methods=list(UNSERVED_METHODS)),
            ]
        )

    async def _unserved(self, request: Request) -> Response:
        route = request.url.path
        if self.ledger is not None:
            self.ledger.refuse(route)
        else:
            self.refused_recordings.append(f"{route} has no provider double")
        return JSONResponse({"error": {"code": 501, "message": f"no double for {route}"}}, 501)

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
        if self.mode == "record":
            return await self._relay(request, route, body)
        if self.ledger is None:
            raise RuntimeError(f"{self.mode} needs a replay ledger")
        answer = self.ledger.respond(route, body)
        if isinstance(answer, ReplayMiss):
            if self.mode == "record_missing" and answer.kind == "unrecorded_request":
                self.ledger.misses.remove(answer)
                return await self._relay(request, route, body)
            return JSONResponse(
                {
                    "error": {
                        "code": MISS_STATUS,
                        "message": f"cassette {answer.kind}",
                        "status": "FAILED_PRECONDITION",
                    }
                },
                status_code=MISS_STATUS,
            )
        return StreamingResponse(
            _replayed(answer.chunks), status_code=answer.status, media_type=answer.media_type
        )

    async def _relay(self, request: Request, route: str, body: bytes) -> Response:
        if self._client is None or self.writer is None or self.budget is None:
            raise RuntimeError(f"{self.mode} relays only while the doubles are running")
        budget = self.budget
        try:
            reservation = await budget.admit(route, body)
        except RecordingStopped as stopped:
            self.refused_recordings.append(f"{route} not relayed: {stopped}")
            return JSONResponse(
                {"error": {"code": MISS_STATUS, "message": str(stopped), "status": "ABORTED"}},
                status_code=MISS_STATUS,
            )
        canonical = canonical_request(route, body)
        headers = {
            name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers
        }
        started = time.monotonic()
        try:
            upstream = await self._client.send(
                self._client.build_request(
                    "POST", GOOGLE + route, params=request.url.query, content=body, headers=headers
                ),
                stream=True,
            )
        except httpx.HTTPError:
            await budget.settle(route, reservation, 0, "")
            raise
        writer = self.writer
        media_type = upstream.headers.get("content-type", "application/json")

        async def relay() -> AsyncIterator[bytes]:
            decoder = codecs.getincrementaldecoder("utf-8")()
            chunks: list[tuple[float, str]] = []
            last = started
            try:
                async for chunk in upstream.aiter_bytes():
                    now = time.monotonic()
                    chunks.append((round(now - last, 4), decoder.decode(chunk)))
                    last = now
                    yield chunk
            finally:
                await upstream.aclose()
                await budget.settle(
                    route, reservation, upstream.status_code, "".join(text for _, text in chunks)
                )
                if 200 <= upstream.status_code < 300:
                    writer.append(
                        model_exchange(canonical, upstream.status_code, media_type, chunks)
                    )
                else:
                    self.refused_recordings.append(f"{route} answered {upstream.status_code}")

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
        elif self.speech is not None:
            await self._record_speech(socket, handshake, hearing, self.speech)
        else:
            await self._replay_muse(socket, hearing)

    def _keep_heard(self, claim: Claim | None, message: Any, *, at: float | None = None) -> None:
        if claim is None or self.writer is None:
            return
        self.writer.append(
            SpeechMessage(
                kind="speech",
                clip=claim.clip,
                take=claim.take,
                offset=round((at or time.monotonic()) - claim.onset, 3),
                message=message,
            )
        )

    async def _relay_muse(self, socket: WebSocket, handshake: str, hearing: _Hearing) -> None:
        query = socket.url.query
        async with websockets.connect(f"{self._stt_upstream}?{query}") as upstream:
            await upstream.send(handshake)
            await socket.send_text(str(await upstream.recv()))

            async def answers() -> None:
                async for raw in upstream:
                    if isinstance(raw, str):
                        message = json.loads(raw)
                        self.transcripts.note(message)
                        self._keep_heard(hearing.claim, message)
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

    def _keep_speech(self, claim: Claim | None, message: Any, *, at: float | None = None) -> None:
        if claim is None or self.speech is None:
            return
        self._speech_messages.setdefault((claim.clip, claim.take), []).append(message)
        self._keep_heard(claim, message, at=at)

    async def _record_speech(
        self, socket: WebSocket, handshake: str, hearing: _Hearing, speech: SpeechRecording
    ) -> None:
        try:
            request = json.loads(handshake)
            session = await speech.open_session(request if isinstance(request, dict) else {})
            if self._stt_key is None:
                raise SpeechStopped("unexpected_handshake", "no live speech key")
            request["authorization"] = {"accessToken": f"Bearer {self._stt_key.get_secret_value()}"}
            async with asyncio.timeout(speech.remaining_wall_seconds):
                await self._relay_speech(socket, request, hearing, session)
        except SessionRefused:
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=1011)
        except (
            SpeechStopped,
            TimeoutError,
            ValueError,
            OSError,
            websockets.WebSocketException,
        ) as error:
            if isinstance(error, TimeoutError):
                speech.stop("deadline", f"{speech.max_wall_seconds} s wall clock spent")
            elif not isinstance(error, SpeechStopped):
                speech.stop("provider_closed", type(error).__name__)
            self.refused_recordings.append(f"speech not recorded: {speech.stop_reason}")
            with suppress(RuntimeError, WebSocketDisconnect):
                await socket.close(code=1011)

    async def _relay_speech(
        self,
        socket: WebSocket,
        request: dict[str, Any],
        hearing: _Hearing,
        session: SpeechSession,
    ) -> None:
        speech = session.recording
        async with websockets.connect(f"{self.stt_upstream_url}?{socket.url.query}") as upstream:
            await upstream.send(json.dumps(request))
            await socket.send_text(str(await upstream.recv()))

            async def answers() -> None:
                async for raw in upstream:
                    if isinstance(raw, str):
                        message = json.loads(raw)
                        speech.provider_message(message)
                        self.transcripts.note(message)
                        self._keep_speech(hearing.claim, message)
                        await socket.send_text(raw)
                speech.provider_closed(session, upstream.close_code, expected=session.closing)

            answering = asyncio.ensure_future(answers())
            try:
                while (message := await socket.receive())["type"] != "websocket.disconnect":
                    _raise_if_failed(answering)
                    await self._forward_worker(message, upstream, hearing, session)
                speech.worker_closing(session)
                _raise_if_failed(answering)
            finally:
                answering.cancel()
                with suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await answering
        speech.provider_closed(session, upstream.close_code, expected=True)

    async def _forward_worker(
        self,
        message: Mapping[str, Any],
        upstream: websockets.ClientConnection,
        hearing: _Hearing,
        session: SpeechSession,
    ) -> None:
        speech = session.recording
        if audio := message.get("bytes"):
            speech.admit_audio(session, audio)
            heard = hearing.hear(audio)
            if heard is not None and heard[0] == "start":
                speech.admit_claim(heard[1].clip, heard[1].take)
            elif heard is not None:
                self._keep_speech(heard[1], {"type": VOICE_END}, at=heard[1].end)
            await upstream.send(audio)
        elif text := message.get("text"):
            if '"endStream"' in text:
                speech.worker_closing(session)
            await upstream.send(text)

    async def _replay_muse(self, socket: WebSocket, hearing: _Hearing) -> None:
        if self.ledger is None:
            raise RuntimeError(f"{self.mode} needs a replay ledger")
        await socket.send_json({"sessionId": f"double-{id(hearing)}"})
        speaking: list[asyncio.Task[None]] = []
        try:
            while (message := await socket.receive())["type"] != "websocket.disconnect":
                audio = message.get("bytes")
                heard = hearing.hear(audio) if audio else None
                if heard is None or heard[0] != "start":
                    continue
                recorded = self.ledger.speech(heard[1].clip, heard[1].take)
                if isinstance(recorded, ReplayMiss):
                    continue
                speaking.append(asyncio.ensure_future(self._spoken(socket, heard[1], recorded)))
        finally:
            for task in speaking:
                task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*speaking, return_exceptions=True)

    async def _spoken(
        self, socket: WebSocket, claim: Claim, messages: tuple[SpeechMessage, ...]
    ) -> None:
        marker = next(
            (line.offset for line in messages if line.message.get("type") == VOICE_END), None
        )
        for line in messages:
            if line.message.get("type") == VOICE_END:
                continue
            due = claim.onset + line.offset
            if marker is not None and line.offset >= marker:
                try:
                    await asyncio.wait_for(
                        claim.ended.wait(), max(0.0, due - time.monotonic()) + END_GRACE_SECONDS
                    )
                except TimeoutError:
                    self.unheard_voice_ends.append(f"{claim.clip} take {claim.take}")
                if claim.end is not None:
                    due = claim.end + line.offset - marker
            delay = due - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.transcripts.note(line.message)
            with suppress(WebSocketDisconnect, RuntimeError):
                await socket.send_json(line.message)

    @property
    def reaches_providers(self) -> bool:
        return self._client is not None

    def report(self) -> dict[str, object]:
        ledger = self.ledger.summary(self.transcripts) if self.ledger is not None else None
        return {
            "mode": self.mode,
            "reaches_providers": self.reaches_providers,
            "cassette": self.cassette.name,
            "cassette_sha256": hashlib.sha256(self.cassette.read_bytes()).hexdigest()
            if self.cassette.exists()
            else None,
            "replay": ledger,
            "voice_ends_not_heard": list(self.unheard_voice_ends),
            "provider_errors_not_recorded": list(self.refused_recordings),
            "recording": self.budget.ledger.report() if self.budget is not None else None,
            "speech_recording": self.speech.report() if self.speech is not None else None,
        }

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
        async with AsyncExitStack() as stack:
            if self.mode != "replay":
                self._client = await stack.enter_async_context(
                    httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
                )
            try:
                await _started(server, serving)
                port = server.servers[0].sockets[0].getsockname()[1]
                self.base_url = f"http://127.0.0.1:{port}"
                yield self
            finally:
                server.should_exit = True
                with suppress(asyncio.CancelledError):
                    await serving
                self.finish_recording()

    def finish_recording(self) -> None:
        if self.mode != "record_missing" or self.ledger is None or self.writer is None:
            return
        if self.speech is not None:
            try:
                self.speech.verify(self._speech_messages)
            except SpeechStopped:
                if f"speech not recorded: {self.speech.stop_reason}" not in self.refused_recordings:
                    self.refused_recordings.append(
                        f"speech not recorded: {self.speech.stop_reason}"
                    )
                self.writer.rewrite(self.ledger.recorded_lines())
                return
        heard = self.ledger.heard
        speech = [
            line
            for takes in self.ledger.takes.values()
            for take in takes
            if (take[0].clip, take[0].take) in heard
            for line in take
        ]
        self.writer.rewrite([*speech, *self.ledger.used_exchanges(), *self.writer.appended])


def _raise_if_failed(task: asyncio.Future[None]) -> None:
    if task.done():
        task.result()


async def _started(server: uvicorn.Server, serving: asyncio.Future[None]) -> None:
    try:
        async with asyncio.timeout(SERVER_START_SECONDS):
            while not server.started:
                if serving.done():
                    serving.result()
                    raise RuntimeError("the provider doubles stopped before they started")
                await asyncio.sleep(0.05)
    except TimeoutError as error:
        raise TimeoutError(
            f"the provider doubles did not listen within {SERVER_START_SECONDS} seconds"
        ) from error


def recorded_judge(folder: Path = CASSETTES) -> TurnJudgeShape:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    role = snapshot["turn_judge"]["model"] or snapshot["reply"]
    exchanges = (
        line
        for path in sorted(folder.glob("*.jsonl"))
        for line in read_cassette(path)
        if isinstance(line, ModelExchange)
    )
    return TurnJudgeShape.captured(judge_route(role["model"]), exchanges)


def provider_doubles(scenario: str) -> ProviderDoubles:
    mode = provider_mode()
    folder = Path(os.environ.get("SIMULATOR_CASSETTE_DIR", CASSETTES))
    path = folder / f"{scenario}.jsonl"
    if mode == "live":
        return ProviderDoubles(mode, path)
    if mode == "record":
        return ProviderDoubles(
            mode, path, writer=CassetteWriter(path, fresh=True), budget=recording_budget()
        )
    if not path.exists():
        raise CassetteError(f"no cassette for {scenario} at {path}; record it before replaying")
    ledger = ReplayLedger.load(path, recorded_judge(folder))
    if mode == "replay":
        return ProviderDoubles(mode, path, ledger=ledger)
    return ProviderDoubles(
        mode,
        path,
        ledger=ledger,
        writer=CassetteWriter(path, fresh=False),
        budget=recording_budget(),
        speech=speech_recording(ledger),
    )


def speech_recording(ledger: ReplayLedger) -> SpeechRecording | None:
    named = os.environ.get(RECORD_SPEECH_ENV, "")
    allowed = frozenset(clip.strip() for clip in named.split(",") if clip.strip())
    if not allowed:
        return None
    recorded = sorted(clip for clip in allowed if ledger.takes.get(clip))
    if recorded:
        raise ValueError(f"{RECORD_SPEECH_ENV} names clips that already have takes: {recorded}")
    sessions = int(os.environ.get(RECORD_SPEECH_SESSIONS_ENV, "1"))
    return SpeechRecording(allowed, max_sessions=sessions)


def _speech_key(recorded: Settings, live: Settings) -> SecretStr:
    if recorded.stt.url != MUSE_ENDPOINT or recorded.stt.model != MUSE_MODEL:
        raise ValueError(f"speech recording needs {MUSE_MODEL} at {MUSE_ENDPOINT}")
    if live.stt.api_key is None:
        raise ValueError("speech recording needs the Muse key in .env")
    return live.stt.api_key


def recording_budget() -> RecordingBudget:
    path = os.environ.get(RECORDING_BUDGET_ENV)
    if not path:
        raise ValueError(f"recording reaches a paid provider; set {RECORDING_BUDGET_ENV}")
    return RecordingBudget.load(Path(path))


async def _replayed(chunks: Sequence[tuple[float, str]]) -> AsyncIterator[bytes]:
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
