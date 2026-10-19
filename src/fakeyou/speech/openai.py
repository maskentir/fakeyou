from collections.abc import AsyncGenerator, AsyncIterator
from typing import cast

from openai import AsyncOpenAI
from openai.types.audio.speech_create_params import SpeechCreateParams, Voice

from fakeyou.config.settings import DEFAULTS, OpenAITTS
from fakeyou.observability.journal import Journal
from fakeyou.speech.languages import SPEECH_LANGUAGES
from fakeyou.speech.streaming import (
    TTS_HTTP_TIMEOUT,
    TTS_REQUEST_TIMEOUT_SECONDS,
    SpeechTag,
    journaled_stream,
)


def tts_voice_instructions(language: str) -> str:
    return (
        "Voice Affect: Warm and relaxed, like a helpful coworker.\n"
        "Tone: Casual and friendly.\n"
        "Pacing: Steady and conversational.\n"
        f"Language: {SPEECH_LANGUAGES[language].display}."
    )


class OpenAISpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        settings: OpenAITTS | None = None,
        language: str = DEFAULTS.speech.language,
        client: AsyncOpenAI | None = None,
        journal: Journal | None = None,
    ) -> None:
        resolved = settings if settings is not None else OpenAITTS()
        self._voice = resolved.voice
        self._model = resolved.model
        self._instructions = tts_voice_instructions(language)
        self._journal = journal
        self._owns_client = client is None
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=resolved.base_url, timeout=TTS_HTTP_TIMEOUT, max_retries=0
        )

    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        return journaled_stream(
            self._journal,
            text,
            self._stream_pcm,
            SpeechTag("openai", sentence_index, transcript_id),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.close()
            self._owns_client = False

    @property
    def client(self) -> AsyncOpenAI:
        return self._client

    @property
    def model(self) -> str:
        return self._model

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def journal(self) -> Journal | None:
        return self._journal

    def _arguments(self, text: str) -> SpeechCreateParams:
        arguments: SpeechCreateParams = {
            "model": self._model,
            "voice": cast(Voice, self._voice),
            "input": text,
            "response_format": "pcm",
        }
        if self._instructions is not None:
            arguments["instructions"] = self._instructions
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
