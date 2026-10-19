import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx2

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import STTSettings, require_secret

_BASE_URL = "https://api.meta.ai/v1"
_MODEL = "muse-voice-transcribe-1.0"
_MODE = "DIARIZATION"
_ENCODING = "WAV"


@dataclass(frozen=True)
class DiarizedTurn:
    start_ms: int
    end_ms: int
    speaker: str


class DiarizeError(FakeYouError):
    pass


def _request_fields(chunk: bytes) -> dict[str, Any]:
    payload = json.dumps({"model": _MODEL, "mode": _MODE, "audioEncoding": _ENCODING})
    return {
        "request": (None, payload, "application/json"),
        "audio": ("chunk.wav", chunk, "audio/wav"),
    }


def _retryable(status: int | None) -> bool:
    return status is None or status == 429 or status >= 500


@dataclass(frozen=True)
class _Post:
    url: str
    headers: dict[str, str]
    files: dict[str, Any]
    timeout_seconds: float
    max_retries: int


def _post_chunk(client: httpx2.Client, post: _Post, *, sleep: Callable[[float], None]) -> bytes:
    attempt = 0
    while True:
        status: int | None = None
        try:
            response = client.post(
                post.url, headers=post.headers, files=post.files, timeout=post.timeout_seconds
            )
        except httpx2.TransportError as error:
            reason = type(error).__name__
        else:
            if response.status_code < 400:
                return response.content
            status = response.status_code
            reason = "HTTPError"
        if attempt >= post.max_retries or not _retryable(status):
            raise DiarizeError(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED, reason=reason)
        sleep(2.0**attempt)
        attempt += 1


def transcribe_chunk(
    chunk: bytes,
    settings: STTSettings,
    *,
    base_url: str = _BASE_URL,
    transport: httpx2.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[DiarizedTurn]:
    api_key = require_secret(settings.api_key, ConfigKey.STT_API_KEY)
    with httpx2.Client(transport=transport) as client:
        post = _Post(
            url=f"{base_url}/asr/transcribe",
            headers={"Authorization": f"Bearer {api_key}"},
            files=_request_fields(chunk),
            timeout_seconds=settings.diarize_timeout_seconds,
            max_retries=settings.diarize_max_retries,
        )
        raw = _post_chunk(client, post, sleep=sleep)
    try:
        payload = json.loads(raw.decode("utf-8"))
        return [
            DiarizedTurn(start_ms=turn["startMs"], end_ms=turn["endMs"], speaker=turn["speaker"])
            for turn in payload["turns"]
        ]
    except (ValueError, KeyError, TypeError) as error:
        raise DiarizeError(ErrorCode.DIARIZE_TRANSCRIPTION_RESPONSE_INVALID) from error


def merge_short_turns(
    turns: list[DiarizedTurn], *, max_ms: int = 2000, gap_ms: int = 1000
) -> list[DiarizedTurn]:
    merged = list(turns)
    index = 0
    while index < len(merged):
        current = merged[index]
        if current.end_ms - current.start_ms >= max_ms:
            index += 1
            continue
        gaps: list[tuple[int, int]] = []
        if index > 0:
            gaps.append((current.start_ms - merged[index - 1].end_ms, index - 1))
        if index + 1 < len(merged):
            gaps.append((merged[index + 1].start_ms - current.end_ms, index + 1))
        near = [item for item in gaps if item[0] < gap_ms]
        if not near:
            index += 1
            continue
        neighbor = min(near)[1]
        other = merged[neighbor]
        merged[min(index, neighbor)] = DiarizedTurn(
            start_ms=min(current.start_ms, other.start_ms),
            end_ms=max(current.end_ms, other.end_ms),
            speaker=other.speaker,
        )
        del merged[max(index, neighbor)]
        index = max(min(index, neighbor) - 1, 0)
    return merged
