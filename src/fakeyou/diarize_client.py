import json
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_BASE_URL = "https://api.meta.ai/v1"
_MODEL = "muse-voice-transcribe-1.0"
_MODE = "DIARIZATION"
_ENCODING = "WAV"


@dataclass(frozen=True)
class DiarizedTurn:
    start_ms: int
    end_ms: int
    speaker: str


class DiarizeError(Exception):
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


def transcribe_chunk(
    chunk: bytes,
    *,
    api_key: str,
    base_url: str = _BASE_URL,
    opener: Callable[[urllib.request.Request], Any] | None = None,
) -> list[DiarizedTurn]:
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
    try:
        with call(request) as response:
            raw = response.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as error:
        raise DiarizeError(f"transcription request failed: {error}") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
        return [
            DiarizedTurn(start_ms=turn["startMs"], end_ms=turn["endMs"], speaker=turn["speaker"])
            for turn in payload["turns"]
        ]
    except (ValueError, KeyError, TypeError) as error:
        raise DiarizeError("transcription response parse failed") from error


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
