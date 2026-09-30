import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from contextlib import aclosing
from typing import Any, Literal, cast

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, Omit
from openai.types.audio.speech_create_params import Voice
from pipecat.services.meta.stt import META_STT_MODEL, MetaSTTService
from pipecat.services.openai.tts import VALID_VOICES
from pipecat.transcriptions.language import Language

from fakeyou.journal import ProviderJournal
from fakeyou.provider_faults import classify_exception, classify_minimax_status
from fakeyou.settings import TTSProvider

MuseMode = Literal["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"]

TTS_HTTP_TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
TTS_REQUEST_TIMEOUT_SECONDS = 45.0
TTS_SAMPLE_RATE = 24_000
TTS_VOICE = "alloy"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE_INSTRUCTIONS = (
    "Voice Affect: Warm and relaxed, like a helpful coworker.\n"
    "Tone: Casual and friendly.\n"
    "Pacing: Steady and conversational.\n"
    "Language: Bahasa Indonesia."
)


MINIMAX_TTS_URL = "https://api.minimax.io/v1/t2a_v2"
MINIMAX_TTS_MODEL = "speech-2.8-turbo"
MINIMAX_TTS_VOICE = "Indonesian_CalmWoman"
MINIMAX_LANGUAGE_BOOST = "Indonesian"
TTS_DEFAULTS: dict[TTSProvider, tuple[str, str]] = {
    "openai": (TTS_MODEL, TTS_VOICE),
    "minimax": (MINIMAX_TTS_MODEL, MINIMAX_TTS_VOICE),
}


def create_tts_http_client() -> DefaultAsyncHttpxClient:
    return DefaultAsyncHttpxClient(timeout=TTS_HTTP_TIMEOUT)


def create_muse_stt(
    *,
    api_key: str,
    language: Language | str | None = Language.ID,
    language_bias: Sequence[Language] | None = None,
    keywords: Sequence[str] | None = None,
    mode: MuseMode = "ENDPOINTING",
    model: str = META_STT_MODEL,
    sample_rate: int | None = None,
    service_class: type[MetaSTTService] = MetaSTTService,
) -> MetaSTTService:
    if mode not in {"ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"}:
        raise ValueError(f"unsupported Muse mode: {mode}")
    settings = MetaSTTService.Settings(
        model=model,
        language=language,
        language_bias=list(language_bias) if language_bias is not None else None,
        keywords=list(keywords) if keywords is not None else None,
        mode=mode,
        emit_audio_progress=False,
    )
    return service_class(api_key=api_key, sample_rate=sample_rate, settings=settings)


class OpenAISpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice: str = TTS_VOICE,
        model: str = TTS_MODEL,
        instructions: str | None = TTS_VOICE_INSTRUCTIONS,
        speed: float | None = None,
        client: AsyncOpenAI | None = None,
        journal: ProviderJournal | None = None,
    ) -> None:
        if voice not in VALID_VOICES:
            raise ValueError(f"unsupported OpenAI voice: {voice}")
        self._voice = voice
        self._model = model
        self._instructions = instructions
        self._speed = speed
        self._journal = journal
        self._http_client: DefaultAsyncHttpxClient | None
        if client is None:
            self._http_client = create_tts_http_client()
            self._client = AsyncOpenAI(
                api_key=api_key, http_client=self._http_client, max_retries=0
            )
        else:
            self._http_client = None
            self._client = client

    async def synthesize(self, text: str) -> bytes:
        return await _journaled_synthesis(self._journal, text, self._request_pcm)

    def stream(self, text: str) -> AsyncIterator[bytes]:
        return _journaled_stream(self._journal, text, self._stream_pcm)

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def _request_pcm(self, text: str) -> bytes:
        response = await self._client.audio.speech.create(
            model=self._model,
            voice=cast(Voice, self._voice),
            input=text,
            instructions=self._instructions if self._instructions is not None else Omit(),
            response_format="pcm",
            speed=self._speed if self._speed is not None else Omit(),
            timeout=TTS_REQUEST_TIMEOUT_SECONDS,
        )
        return response.read()

    async def _stream_pcm(self, text: str) -> AsyncGenerator[bytes]:
        async with self._client.audio.speech.with_streaming_response.create(
            model=self._model,
            voice=cast(Voice, self._voice),
            input=text,
            instructions=self._instructions if self._instructions is not None else Omit(),
            response_format="pcm",
            stream_format="audio",
            speed=self._speed if self._speed is not None else Omit(),
            timeout=TTS_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            async for chunk in response.iter_bytes():
                yield chunk


class MiniMaxSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        voice: str = MINIMAX_TTS_VOICE,
        model: str = MINIMAX_TTS_MODEL,
        language_boost: str = MINIMAX_LANGUAGE_BOOST,
        url: str = MINIMAX_TTS_URL,
        client: httpx2.AsyncClient | None = None,
        journal: ProviderJournal | None = None,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._voice = voice
        self._model = model
        self._language_boost = language_boost
        self._url = url
        self._journal = journal
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(timeout=TTS_HTTP_TIMEOUT)

    async def synthesize(self, text: str) -> bytes:
        return await _journaled_synthesis(self._journal, text, self._request_pcm)

    def stream(self, text: str) -> AsyncIterator[bytes]:
        return _journaled_stream(self._journal, text, self._stream_pcm)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            self._owns_client = False

    def _payload(self, text: str, *, stream: bool) -> dict[str, Any]:
        return {
            "model": self._model,
            "text": text,
            "stream": stream,
            "language_boost": self._language_boost,
            "voice_setting": {"voice_id": self._voice},
            "audio_setting": {"sample_rate": TTS_SAMPLE_RATE, "format": "pcm", "channel": 1},
            **({"stream_options": {"exclude_aggregated_audio": True}} if stream else {}),
        }

    async def _request_pcm(self, text: str) -> bytes:
        response = await self._client.post(
            self._url, headers=self._headers, json=self._payload(text, stream=False)
        )
        response.raise_for_status()
        return _minimax_audio(response.json()) or b""

    async def _stream_pcm(self, text: str) -> AsyncGenerator[bytes]:
        async with self._client.stream(
            "POST", self._url, headers=self._headers, json=self._payload(text, stream=True)
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line.removeprefix("data:"))
                if event.get("extra_info") is not None:
                    _minimax_audio(event)
                    return
                audio = _minimax_audio(event)
                if audio:
                    yield audio


def create_speech_synthesizer(
    provider: TTSProvider,
    *,
    api_key: str,
    voice: str | None = None,
    journal: ProviderJournal | None = None,
) -> OpenAISpeechSynthesizer | MiniMaxSpeechSynthesizer:
    model, default_voice = TTS_DEFAULTS[provider]
    if provider == "minimax":
        return MiniMaxSpeechSynthesizer(
            api_key=api_key, voice=voice or default_voice, model=model, journal=journal
        )
    return OpenAISpeechSynthesizer(
        api_key=api_key, voice=voice or default_voice, model=model, journal=journal
    )


def _minimax_audio(body: dict[str, Any]) -> bytes | None:
    status = body.get("base_resp") or {}
    code = status.get("status_code", 0)
    if code:
        raise classify_minimax_status("tts", code, status.get("status_msg"))
    audio = (body.get("data") or {}).get("audio")
    return bytes.fromhex(audio) if audio else None


async def _journaled_synthesis(
    journal: ProviderJournal | None, text: str, request: Callable[[str], Awaitable[bytes]]
) -> bytes:
    if not text.strip():
        raise ValueError("cannot synthesize blank text")
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    if journal is not None:
        journal.record("synth_started", request_id=request_id, chars=len(text))
    try:
        audio = await request(text)
    except Exception as error:
        fault = classify_exception("tts", error)
        if journal is not None:
            journal.record(
                "synth_failed",
                request_id=request_id,
                kind=fault.kind,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        raise fault from error
    if journal is not None:
        journal.record(
            "synth_finished",
            request_id=request_id,
            chars=len(text),
            bytes_out=len(audio),
            duration_ms=(time.monotonic() - started) * 1000,
        )
    return audio


async def _journaled_stream(
    journal: ProviderJournal | None, text: str, open_stream: Callable[[str], AsyncGenerator[bytes]]
) -> AsyncGenerator[bytes]:
    if not text.strip():
        raise ValueError("cannot synthesize blank text")
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    first_chunk_at: float | None = None
    total = 0
    if journal is not None:
        journal.record("synth_started", request_id=request_id, chars=len(text), streaming=True)
    try:
        async with aclosing(open_stream(text)) as chunks:
            async for chunk in chunks:
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                total += len(chunk)
                yield chunk
    except asyncio.CancelledError:
        raise
    except Exception as error:
        fault = classify_exception("tts", error)
        if journal is not None:
            journal.record(
                "synth_failed",
                request_id=request_id,
                kind=fault.kind,
                bytes_out=total,
                first_chunk_latency_ms=(first_chunk_at - started) * 1000
                if first_chunk_at
                else None,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        raise fault from error
    if journal is not None:
        journal.record(
            "synth_finished",
            request_id=request_id,
            chars=len(text),
            bytes_out=total,
            first_chunk_latency_ms=(first_chunk_at - started) * 1000 if first_chunk_at else None,
            duration_ms=(time.monotonic() - started) * 1000,
            streaming=True,
        )
