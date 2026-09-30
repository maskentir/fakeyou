import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Literal, cast

import httpx2
from openai import AsyncOpenAI, DefaultAsyncHttpxClient, Omit
from openai.types.audio.speech_create_params import Voice
from pipecat.services.meta.stt import META_STT_MODEL, MetaSTTService
from pipecat.services.openai.tts import VALID_VOICES
from pipecat.transcriptions.language import Language

from fakeyou.journal import ProviderJournal
from fakeyou.provider_faults import classify_exception

MuseMode = Literal["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"]

TTS_HTTP_TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
TTS_REQUEST_TIMEOUT_SECONDS = 45.0
TTS_VOICE = "alloy"
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE_INSTRUCTIONS = (
    "Voice Affect: Warm and relaxed, like a helpful coworker.\n"
    "Tone: Casual and friendly.\n"
    "Pacing: Steady and conversational.\n"
    "Language: Bahasa Indonesia."
)


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
        if not text.strip():
            raise ValueError("cannot synthesize blank text")
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        if self._journal is not None:
            self._journal.record("synth_started", request_id=request_id, chars=len(text))
        try:
            response = await self._client.audio.speech.create(
                model=self._model,
                voice=cast(Voice, self._voice),
                input=text,
                instructions=self._instructions if self._instructions is not None else Omit(),
                response_format="pcm",
                speed=self._speed if self._speed is not None else Omit(),
                timeout=TTS_REQUEST_TIMEOUT_SECONDS,
            )
            audio = response.read()
        except Exception as error:
            fault = classify_exception("tts", error)
            if self._journal is not None:
                self._journal.record(
                    "synth_failed",
                    request_id=request_id,
                    kind=fault.kind,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            raise fault from error
        if self._journal is not None:
            self._journal.record(
                "synth_finished",
                request_id=request_id,
                chars=len(text),
                bytes_out=len(audio),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        return audio

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            raise ValueError("cannot synthesize blank text")
        started = time.monotonic()
        request_id = uuid.uuid4().hex
        first_chunk_at: float | None = None
        total = 0
        if self._journal is not None:
            self._journal.record(
                "synth_started", request_id=request_id, chars=len(text), streaming=True
            )
        try:
            response_context = self._client.audio.speech.with_streaming_response.create(
                model=self._model,
                voice=cast(Voice, self._voice),
                input=text,
                instructions=self._instructions if self._instructions is not None else Omit(),
                response_format="pcm",
                stream_format="audio",
                speed=self._speed if self._speed is not None else Omit(),
                timeout=TTS_REQUEST_TIMEOUT_SECONDS,
            )
            async with response_context as response:
                async for chunk in response.iter_bytes():
                    if first_chunk_at is None:
                        first_chunk_at = time.monotonic()
                    total += len(chunk)
                    yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as error:
            fault = classify_exception("tts", error)
            if self._journal is not None:
                self._journal.record(
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
        if self._journal is not None:
            self._journal.record(
                "synth_finished",
                request_id=request_id,
                chars=len(text),
                bytes_out=total,
                first_chunk_latency_ms=(first_chunk_at - started) * 1000
                if first_chunk_at
                else None,
                duration_ms=(time.monotonic() - started) * 1000,
                streaming=True,
            )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
