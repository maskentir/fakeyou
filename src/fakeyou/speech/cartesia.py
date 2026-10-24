from collections.abc import AsyncGenerator, AsyncIterator
from functools import partial

import httpx2

from fakeyou.config.settings import DEFAULTS, CartesiaTTS
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_http
from fakeyou.speech.streaming import (
    TTS_HTTP_TIMEOUT,
    TTS_REQUEST_TIMEOUT_SECONDS,
    SpeechTag,
    journaled_stream,
)

CARTESIA_API_VERSION = "2026-03-01"


class CartesiaSpeechSynthesizer:
    def __init__(
        self,
        *,
        api_key: str,
        settings: CartesiaTTS | None = None,
        language: str = DEFAULTS.speech.language,
        client: httpx2.AsyncClient | None = None,
        journal: Journal | None = None,
    ) -> None:
        resolved = settings if settings is not None else CartesiaTTS()
        self._headers = {"X-API-Key": api_key, "Cartesia-Version": CARTESIA_API_VERSION}
        self._url = f"{resolved.base_url}/tts/bytes"
        self._model = resolved.model
        self._voice = resolved.voice
        self._language = language
        self._journal = journal
        self._owns_client = client is None
        self._client = client or httpx2.AsyncClient(timeout=TTS_HTTP_TIMEOUT)

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
            SpeechTag("cartesia", sentence_index, transcript_id),
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
    def journal(self) -> Journal | None:
        return self._journal

    def _payload(self, text: str, language: str) -> dict[str, object]:
        return {
            "model_id": self._model,
            "transcript": text,
            "voice": {"mode": "id", "id": self._voice},
            "language": language,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": PLAYBACK_SAMPLE_RATE,
            },
        }

    async def _stream_pcm(self, text: str, language: str) -> AsyncGenerator[bytes]:
        async with self._client.stream(
            "POST",
            self._url,
            headers=self._headers,
            json=self._payload(text, language),
            timeout=TTS_REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise classify_http("tts", response.status_code, response.text)
            async for chunk in response.aiter_bytes():
                yield chunk
