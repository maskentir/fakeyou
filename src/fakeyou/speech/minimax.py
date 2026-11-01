import json
from collections.abc import AsyncGenerator, AsyncIterator
from functools import partial
from typing import Any

import httpx2

from fakeyou.config.settings import DEFAULTS, MiniMaxTTS
from fakeyou.foundation.invariants import PCM_CHANNELS, PLAYBACK_SAMPLE_RATE
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_minimax_status
from fakeyou.speech.contract import SpeechTurn
from fakeyou.speech.languages import SPEECH_LANGUAGES
from fakeyou.speech.streaming import (
    TTS_HTTP_TIMEOUT,
    SentenceTurn,
    SpeechTag,
    journaled_stream,
)


def _minimax_audio(body: dict[str, Any]) -> bytes | None:
    status = body.get("base_resp") or {}
    code = status.get("status_code", 0)
    if code:
        raise classify_minimax_status("tts", code, status.get("status_msg"))
    audio = (body.get("data") or {}).get("audio")
    return bytes.fromhex(audio) if audio else None


class MiniMaxSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        settings: MiniMaxTTS | None = None,
        language: str = DEFAULTS.speech.language,
        client: httpx2.AsyncClient | None = None,
        journal: Journal | None = None,
    ) -> None:
        resolved = settings if settings is not None else MiniMaxTTS()
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._voice = resolved.voice
        self._model = resolved.model
        self._language = language
        self._url = resolved.base_url
        self._journal = journal
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(timeout=TTS_HTTP_TIMEOUT)

    def open_turn(
        self, *, transcript_id: str | None = None, language: str | None = None
    ) -> SpeechTurn:
        return SentenceTurn(self.stream, transcript_id=transcript_id, language=language)

    def stream(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> AsyncIterator[bytes]:
        return journaled_stream(
            self._journal,
            text,
            partial(self._stream_pcm, language=language or self._language),
            SpeechTag("minimax", sentence_index, transcript_id),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            self._owns_client = False

    @property
    def client(self) -> httpx2.AsyncClient:
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

    def _payload(self, text: str, language: str) -> dict[str, Any]:
        return {
            "model": self._model,
            "text": text,
            "stream": True,
            "language_boost": SPEECH_LANGUAGES[language].english,
            "voice_setting": {"voice_id": self._voice},
            "audio_setting": {
                "sample_rate": PLAYBACK_SAMPLE_RATE,
                "format": "pcm",
                "channel": PCM_CHANNELS,
            },
            "stream_options": {"exclude_aggregated_audio": True},
        }

    async def _stream_pcm(self, text: str, language: str) -> AsyncGenerator[bytes]:
        async with self._client.stream(
            "POST", self._url, headers=self._headers, json=self._payload(text, language)
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
