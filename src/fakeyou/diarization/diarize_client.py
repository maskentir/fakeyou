import json
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def _encode_request(chunk: bytes) -> tuple[str, bytes]:
    boundary = uuid.uuid4().hex
    payload = json.dumps({"model": _MODEL, "mode": _MODE, "audioEncoding": _ENCODING})
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="request"\r\n'
        f"Content-Type: application/json\r\n\r\n{payload}\r\n"
        f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; '
        f'filename="chunk.wav"\r\nContent-Type: audio/wav\r\n\r\n'
    ).encode("ascii")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    return boundary, head + chunk + tail


def _retryable_http(error: OSError) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return True


def _post_chunk(
    call: Callable[..., Any],
    request: urllib.request.Request,
    *,
    timeout_seconds: float,
    max_retries: int,
    sleep: Callable[[float], None],
) -> bytes:
    attempt = 0
    while True:
        try:
            with call(request, timeout=timeout_seconds) as response:
                payload: bytes = response.read()
                return payload
        except OSError as error:
            if attempt >= max_retries or not _retryable_http(error):
                raise DiarizeError(
                    ErrorCode.DIARIZE_TRANSCRIPTION_FAILED, reason=type(error).__name__
                ) from error
            sleep(2.0**attempt)
            attempt += 1


def transcribe_chunk(
    chunk: bytes,
    settings: STTSettings,
    *,
    base_url: str = _BASE_URL,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> list[DiarizedTurn]:
    api_key = require_secret(settings.api_key, ConfigKey.STT_API_KEY)
    boundary, body = _encode_request(chunk)
    call = urllib.request.urlopen if opener is None else opener
    request = urllib.request.Request(
        f"{base_url}/asr/transcribe",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    raw = _post_chunk(
        call,
        request,
        timeout_seconds=settings.diarize_timeout_seconds,
        max_retries=settings.diarize_max_retries,
        sleep=sleep,
    )
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
