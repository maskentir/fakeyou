import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from fakeyou.diarize_client import DiarizedTurn, DiarizeError, merge_short_turns, transcribe_chunk


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


def test_transcribe_chunk_parses_turns_and_drops_transcript_text() -> None:
    seen: dict[str, Any] = {}

    def opener(request: urllib.request.Request) -> _FakeResponse:
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["content_type"] = request.get_header("Content-type")
        seen["body"] = request.data
        return _FakeResponse(_turn_payload())

    chunk = b"RIFF" + bytes(60)
    turns = transcribe_chunk(chunk, api_key="secret-key", opener=opener)

    assert turns == [
        DiarizedTurn(start_ms=0, end_ms=3400, speaker="A"),
        DiarizedTurn(start_ms=3600, end_ms=7200, speaker="B"),
        DiarizedTurn(start_ms=7300, end_ms=9000, speaker="A"),
    ]
    assert seen["url"] == "https://api.meta.ai/v1/asr/transcribe"
    assert seen["auth"] == "Bearer secret-key"
    assert str(seen["content_type"]).startswith("multipart/form-data; boundary=")
    body = bytes(seen["body"])
    assert chunk in body
    assert b"muse-voice-transcribe-1.0" in body
    assert b"DIARIZATION" in body
    assert b"WAV" in body


def test_transcribe_chunk_http_error_becomes_diarize_error_without_key() -> None:
    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 500, "server blew up", {}, None)

    with pytest.raises(DiarizeError) as group:
        transcribe_chunk(b"RIFF" + bytes(60), api_key="secret-key", opener=opener)

    assert "secret-key" not in str(group.value)


def test_transcribe_chunk_url_error_becomes_diarize_error_without_key() -> None:
    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    with pytest.raises(DiarizeError) as group:
        transcribe_chunk(b"RIFF" + bytes(60), api_key="secret-key", opener=opener)

    assert "secret-key" not in str(group.value)


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
