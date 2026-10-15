import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode
from fakeyou.config.settings import STTSettings
from fakeyou.diarization.diarize_client import (
    DiarizedTurn,
    DiarizeError,
    merge_short_turns,
    transcribe_chunk,
)
from tests._error_support import message_pattern


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _turn_payload() -> bytes:
    return json.dumps(
        {
            "turns": [
                {
                    "turnId": "t1",
                    "startMs": 0,
                    "endMs": 3400,
                    "transcript": "hello there",
                    "speaker": "A",
                },
                {
                    "turnId": "t2",
                    "startMs": 3600,
                    "endMs": 7200,
                    "transcript": "good morning",
                    "speaker": "B",
                },
                {
                    "turnId": "t3",
                    "startMs": 7300,
                    "endMs": 9000,
                    "transcript": "see you",
                    "speaker": "A",
                },
            ]
        }
    ).encode("utf-8")


def _request_part(body: bytes) -> dict[str, Any]:
    marker = b"Content-Type: application/json\r\n\r\n"
    start = body.index(marker) + len(marker)
    end = body.index(b"\r\n--", start)
    parsed: dict[str, Any] = json.loads(body[start:end].decode("utf-8"))
    return parsed


def test_transcribe_chunk_parses_turns_and_drops_transcript_text() -> None:
    seen: dict[str, Any] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del timeout
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        seen["auth"] = request.get_header("Authorization")
        seen["content_type"] = request.get_header("Content-type")
        seen["body"] = request.data
        return _FakeResponse(_turn_payload())

    chunk = b"RIFF" + bytes(60)
    settings = STTSettings(api_key=SecretStr("secret-key"))
    turns = transcribe_chunk(chunk, settings, opener=opener)

    assert turns == [
        DiarizedTurn(start_ms=0, end_ms=3400, speaker="A"),
        DiarizedTurn(start_ms=3600, end_ms=7200, speaker="B"),
        DiarizedTurn(start_ms=7300, end_ms=9000, speaker="A"),
    ]
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["auth"] == "Bearer secret-key"
    assert seen["method"] == "POST"
    assert str(seen["content_type"]).startswith("multipart/form-data; boundary=")
    body = bytes(seen["body"])
    assert chunk in body
    assert b'name="request"' in body
    assert b'name="audio"' in body
    assert _request_part(body) == {
        "model": "muse-voice-transcribe-1.0",
        "mode": "DIARIZATION",
        "audioEncoding": "WAV",
    }


def test_transcribe_chunk_http_error_becomes_diarize_error_without_key() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del timeout
        raise urllib.error.HTTPError(request.full_url, 500, "server blew up", Message(), None)

    settings = STTSettings(api_key=SecretStr("secret-key"), diarize_max_retries=0)
    with pytest.raises(
        DiarizeError, match=message_pattern(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED)
    ) as group:
        transcribe_chunk(b"RIFF" + bytes(60), settings, opener=opener)

    assert "secret-key" not in str(group.value)
    assert group.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_FAILED
    assert str(group.value) == "transcription request failed: HTTPError"


def test_transcribe_chunk_url_error_becomes_diarize_error_without_key() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        raise urllib.error.URLError("connection refused")

    settings = STTSettings(api_key=SecretStr("secret-key"), diarize_max_retries=0)
    with pytest.raises(
        DiarizeError, match=message_pattern(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED)
    ) as group:
        transcribe_chunk(b"RIFF" + bytes(60), settings, opener=opener)

    assert "secret-key" not in str(group.value)
    assert group.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_FAILED
    assert str(group.value) == "transcription request failed: URLError"


@pytest.mark.parametrize(
    "payload",
    [b"this is not json", json.dumps({"status": "ok"}).encode("utf-8")],
)
def test_transcribe_chunk_malformed_body_becomes_diarize_error(payload: bytes) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        return _FakeResponse(payload)

    settings = STTSettings(api_key=SecretStr("dummy-key-1"))
    with pytest.raises(
        DiarizeError, match=message_pattern(ErrorCode.DIARIZE_TRANSCRIPTION_RESPONSE_INVALID)
    ) as group:
        transcribe_chunk(b"RIFF" + bytes(60), settings, opener=opener)

    assert "dummy-key-1" not in str(group.value)
    assert group.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_RESPONSE_INVALID


def test_merge_short_turns_joins_tail_flanked_by_same_speaker() -> None:
    turns = [
        DiarizedTurn(start_ms=0, end_ms=5000, speaker="A"),
        DiarizedTurn(start_ms=5600, end_ms=6800, speaker="B"),
        DiarizedTurn(start_ms=7000, end_ms=12000, speaker="A"),
    ]

    assert merge_short_turns(turns) == [
        DiarizedTurn(start_ms=0, end_ms=5000, speaker="A"),
        DiarizedTurn(start_ms=5600, end_ms=12000, speaker="A"),
    ]


def test_merge_short_turns_keeps_isolated_short_turn() -> None:
    turns = [
        DiarizedTurn(start_ms=0, end_ms=5000, speaker="A"),
        DiarizedTurn(start_ms=8000, end_ms=9200, speaker="B"),
        DiarizedTurn(start_ms=12000, end_ms=17000, speaker="A"),
    ]

    assert merge_short_turns(turns) == turns


def _budgeted_settings(max_retries: int) -> STTSettings:
    return STTSettings(
        api_key=SecretStr("key"), diarize_timeout_seconds=7.5, diarize_max_retries=max_retries
    )


def _turns_payload(turns: list[tuple[int, int, str]]) -> bytes:
    return json.dumps(
        {
            "turns": [
                {
                    "turnId": f"t{index}",
                    "startMs": start,
                    "endMs": end,
                    "transcript": "discarded words",
                    "speaker": speaker,
                }
                for index, (start, end, speaker) in enumerate(turns)
            ]
        }
    ).encode("utf-8")


def test_transcribe_chunk_passes_timeout_to_the_opener() -> None:
    seen: dict[str, Any] = {}

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        seen["timeout"] = timeout
        return _FakeResponse(_turns_payload([(0, 1000, "A")]))

    turns = transcribe_chunk(b"chunk", _budgeted_settings(0), opener=opener)

    assert [(turn.start_ms, turn.end_ms, turn.speaker) for turn in turns] == [(0, 1000, "A")]
    assert seen["timeout"] == 7.5


def test_transcribe_chunk_retries_transient_failures() -> None:
    calls = 0
    sleeps: list[float] = []

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        nonlocal calls
        calls += 1
        if calls < 3:
            raise urllib.error.URLError("connection reset")
        return _FakeResponse(_turns_payload([(0, 1000, "A")]))

    turns = transcribe_chunk(b"chunk", _budgeted_settings(3), opener=opener, sleep=sleeps.append)

    assert len(turns) == 1
    assert calls == 3
    assert sleeps == [1.0, 2.0]


def test_transcribe_chunk_retries_retryable_http_status() -> None:
    calls = 0

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError("http://x", 503, "busy", Message(), None)
        return _FakeResponse(_turns_payload([(0, 1000, "A")]))

    turns = transcribe_chunk(b"chunk", _budgeted_settings(1), opener=opener, sleep=lambda _: None)

    assert len(turns) == 1
    assert calls == 2


def test_transcribe_chunk_fails_fast_on_fatal_http_status() -> None:
    calls = 0

    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        nonlocal calls
        calls += 1
        raise urllib.error.HTTPError("http://x", 400, "bad request", Message(), None)

    with pytest.raises(
        DiarizeError, match=message_pattern(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED)
    ) as error:
        transcribe_chunk(b"chunk", _budgeted_settings(3), opener=opener, sleep=lambda _: None)

    assert error.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_FAILED
    assert str(error.value) == "transcription request failed: HTTPError"
    assert calls == 1


def test_transcribe_chunk_maps_timeout_to_diarize_error() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _FakeResponse:
        del request
        del timeout
        raise TimeoutError("timed out")

    with pytest.raises(
        DiarizeError, match=message_pattern(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED)
    ) as error:
        transcribe_chunk(b"chunk", _budgeted_settings(2), opener=opener, sleep=lambda _: None)

    assert error.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_FAILED
    assert str(error.value) == "transcription request failed: TimeoutError"
