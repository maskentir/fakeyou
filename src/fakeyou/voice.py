import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import aclosing
from typing import Any, cast, get_args

import httpx2
from openai import AsyncOpenAI
from openai.types.audio.speech_create_params import SpeechCreateParams, Voice
from pipecat.services.meta.stt import MetaSTTService
from pipecat.services.openai.tts import VALID_VOICES
from pipecat.transcriptions.language import Language

from fakeyou.invariants import PCM_CHANNELS, PLAYBACK_SAMPLE_RATE
from fakeyou.journal import Journal
from fakeyou.provider_faults import classify_exception, classify_minimax_status
from fakeyou.settings import DEFAULTS, MiniMaxTTS, STTMode, TTSSettings, require_secret

TTS_HTTP_TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
TTS_REQUEST_TIMEOUT_SECONDS = 45.0
SPEECH_LANGUAGES = {
    "id": (Language.ID, "Bahasa Indonesia", "Indonesian"),
    "en": (Language.EN, "English", "English"),
}


def tts_voice_instructions(language: str) -> str:
    return (
        "Voice Affect: Warm and relaxed, like a helpful coworker.\n"
        "Tone: Casual and friendly.\n"
        "Pacing: Steady and conversational.\n"
        f"Language: {SPEECH_LANGUAGES[language][1]}."
    )


def create_muse_stt(
    *,
    api_key: str,
    language: Language | str | None = Language.ID,
    language_bias: Sequence[Language] | None = None,
    keywords: Sequence[str] | None = None,
    mode: STTMode = DEFAULTS.stt.mode,
    model: str = DEFAULTS.stt.model,
    url: str = DEFAULTS.stt.url,
    sample_rate: int | None = None,
    service_class: type[MetaSTTService] = MetaSTTService,
) -> MetaSTTService:
    if mode not in get_args(STTMode):
        raise ValueError(f"unsupported Muse mode: {mode}")
    settings = MetaSTTService.Settings(
        model=model,
        language=language,
        language_bias=list(language_bias) if language_bias is not None else None,
        keywords=list(keywords) if keywords is not None else None,
        mode=mode,
        emit_audio_progress=False,
    )
    return service_class(api_key=api_key, url=url, sample_rate=sample_rate, settings=settings)


class OpenAISpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULTS.tts.base_url,
        voice: str = DEFAULTS.tts.voice,
        model: str = DEFAULTS.tts.model,
        instructions: str | None = tts_voice_instructions(DEFAULTS.speech.language),
        speed: float | None = None,
        client: AsyncOpenAI | None = None,
        journal: Journal | None = None,
    ) -> None:
        if voice not in VALID_VOICES:
            raise ValueError(f"unsupported OpenAI voice: {voice}")
        self._voice = voice
        self._model = model
        self._instructions = instructions
        self._speed = speed
        self._journal = journal
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=TTS_HTTP_TIMEOUT, max_retries=0
        )

    def stream(self, text: str) -> AsyncIterator[bytes]:
        return _journaled_stream(self._journal, text, self._stream_pcm)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()
            self._owns_client = False

    def _arguments(self, text: str) -> SpeechCreateParams:
        arguments: SpeechCreateParams = {
            "model": self._model,
            "voice": cast(Voice, self._voice),
            "input": text,
            "response_format": "pcm",
        }
        if self._instructions is not None:
            arguments["instructions"] = self._instructions
        if self._speed is not None:
            arguments["speed"] = self._speed
        return arguments

    async def _stream_pcm(self, text: str) -> AsyncGenerator[bytes]:
        arguments = self._arguments(text)
        arguments["stream_format"] = "audio"
        async with self._client.audio.speech.with_streaming_response.create(
            **arguments,
            timeout=TTS_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            async for chunk in response.iter_bytes():
                yield chunk


class MiniMaxSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        voice: str = MiniMaxTTS().voice,
        model: str = MiniMaxTTS().model,
        language_boost: str = SPEECH_LANGUAGES[DEFAULTS.speech.language][2],
        url: str = MiniMaxTTS().base_url,
        client: httpx2.AsyncClient | None = None,
        journal: Journal | None = None,
    ) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._voice = voice
        self._model = model
        self._language_boost = language_boost
        self._url = url
        self._journal = journal
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(timeout=TTS_HTTP_TIMEOUT)

    def stream(self, text: str) -> AsyncIterator[bytes]:
        return _journaled_stream(self._journal, text, self._stream_pcm)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            self._owns_client = False

    def _payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "text": text,
            "stream": True,
            "language_boost": self._language_boost,
            "voice_setting": {"voice_id": self._voice},
            "audio_setting": {
                "sample_rate": PLAYBACK_SAMPLE_RATE,
                "format": "pcm",
                "channel": PCM_CHANNELS,
            },
            "stream_options": {"exclude_aggregated_audio": True},
        }

    async def _stream_pcm(self, text: str) -> AsyncGenerator[bytes]:
        async with self._client.stream(
            "POST", self._url, headers=self._headers, json=self._payload(text)
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
    settings: TTSSettings,
    *,
    language: str = DEFAULTS.speech.language,
    journal: Journal | None = None,
) -> OpenAISpeechSynthesizer | MiniMaxSpeechSynthesizer:
    api_key = require_secret(settings.api_key, "FAKEYOU_TTS__API_KEY")
    if isinstance(settings, MiniMaxTTS):
        return MiniMaxSpeechSynthesizer(
            api_key=api_key,
            voice=settings.voice,
            model=settings.model,
            url=settings.base_url,
            language_boost=SPEECH_LANGUAGES[language][2],
            journal=journal,
        )
    return OpenAISpeechSynthesizer(
        api_key=api_key,
        voice=settings.voice,
        model=settings.model,
        base_url=settings.base_url,
        instructions=tts_voice_instructions(language),
        journal=journal,
    )


def _minimax_audio(body: dict[str, Any]) -> bytes | None:
    status = body.get("base_resp") or {}
    code = status.get("status_code", 0)
    if code:
        raise classify_minimax_status("tts", code, status.get("status_msg"))
    audio = (body.get("data") or {}).get("audio")
    return bytes.fromhex(audio) if audio else None


async def _journaled_stream(
    journal: Journal | None, text: str, open_stream: Callable[[str], AsyncGenerator[bytes]]
) -> AsyncGenerator[bytes]:
    if not text.strip():
        raise ValueError("cannot synthesize blank text")
    journal = journal or Journal("", stdout=False)
    with journal.timed(
        started="synthesis_started",
        finished="synthesis_finished",
        failed="synthesis_failed",
        source="tts",
        chars=len(text),
        streaming=True,
    ) as timing:
        timing.fields.update(bytes_out=0, first_chunk_ms=None)
        try:
            async with aclosing(open_stream(text)) as chunks:
                async for chunk in chunks:
                    if timing.fields["first_chunk_ms"] is None:
                        timing.fields["first_chunk_ms"] = timing.elapsed_ms
                    timing.fields["bytes_out"] += len(chunk)
                    yield chunk
        except Exception as error:
            fault = classify_exception("tts", error)
            if fault is error:
                raise
            raise fault from error
