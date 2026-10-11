import json
import struct
import sys
import urllib.request
import wave
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.diarize import diarize_run, diarize_show
from fakeyou.config.settings import Settings
from fakeyou.diarization.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.diarization.diarize_correction import write_correction_bundle
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.persistence.ingestion_status import lock_path, record_lock
from fakeyou.persistence.record import meeting_record_path

SECRET_KEY = "super-secret-stt-key"


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def _write_slot_wav(path: Path, samples: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CAPTURE_SAMPLE_RATE)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _write_session(
    settings: Settings, meeting_id: str, *, seq_starts: dict[int, int], frames: int = 1600
) -> tuple[Path, Path]:
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text('{"kind": "resume", "at": "2026-10-08T00:00:00+00:00"}\n')
    audio_dir = record_path.parent / "audio"
    slots = {
        str(slot): {
            "seq_start": start,
            "seq_end": start + 10,
            "frames": frames,
            "gaps_filled": 0,
        }
        for slot, start in seq_starts.items()
    }
    (audio_dir).mkdir(parents=True, exist_ok=True)
    (audio_dir / "slots.json").write_text(
        json.dumps({"schema": "slot_audio/v1", "slots": slots, "anchors": []}),
        encoding="utf-8",
    )
    for slot in seq_starts:
        _write_slot_wav(audio_dir / f"slot-{slot}.wav", [100] * frames)
    return record_path, audio_dir


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


def test_diarize_run_refuses_missing_api_key(settings: Settings) -> None:
    assert settings.stt.api_key is None

    with pytest.raises(FakeYouError, match="FAKEYOU_STT__API_KEY") as error:
        diarize_run("m1", settings=settings)

    assert error.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_diarize_run_maps_client_error_without_secret_text(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    _write_session(settings, "m1", seq_starts={0: 0})

    def opener(request: urllib.request.Request) -> _FakeResponse:
        return _FakeResponse(b"not-json{{{")

    with pytest.raises(DiarizeError) as error:
        diarize_run("m1", settings=settings, opener=opener)

    assert SECRET_KEY not in str(error.value)
    assert error.value.code is ErrorCode.DIARIZE_TRANSCRIPTION_RESPONSE_INVALID
    assert isinstance(error.value.__cause__, ValueError)


def test_diarize_run_refuses_concurrent_run_via_lease(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, _ = _write_session(settings, "m1", seq_starts={0: 0})

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("locked run must not transcribe")

    with record_lock(record_path, purpose="meeting"):
        with pytest.raises(FakeYouError, match="locked") as error:
            diarize_run("m1", settings=settings, opener=opener)

    assert error.value.code is ErrorCode.RECORD_LOCKED


def test_diarize_run_releases_diarization_lease_on_success(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, _ = _write_session(settings, "m1", seq_starts={0: 0})

    def opener(request: urllib.request.Request) -> _FakeResponse:
        return _FakeResponse(_turns_payload([(0, 5000, "A")]))

    assert diarize_run("m1", settings=settings, opener=opener) == 0
    assert not lock_path(record_path).exists()


def test_diarize_run_refuses_unequal_seq_start(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    _write_session(settings, "m1", seq_starts={0: 0, 1: 5})

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("misaligned run must not transcribe")

    with pytest.raises(FakeYouError, match="misaligned") as error:
        diarize_run("m1", settings=settings, opener=opener)

    assert error.value.code is ErrorCode.DIARIZE_SLOT_MISALIGNED


def test_diarize_run_refuses_nonzero_seq_start(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    _write_session(settings, "m1", seq_starts={0: 3, 1: 3})

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("misaligned run must not transcribe")

    with pytest.raises(FakeYouError, match="zero") as error:
        diarize_run("m1", settings=settings, opener=opener)

    assert error.value.code is ErrorCode.DIARIZE_SLOT_NOT_AT_ZERO


def test_diarize_run_maps_empty_audio_dir_to_input_unreadable(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path = meeting_record_path(settings.paths.record_dir, "m1")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text("{}\n")
    (record_path.parent / "audio").mkdir(parents=True, exist_ok=True)

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("empty run must not transcribe")

    with pytest.raises(FakeYouError) as error:
        diarize_run("m1", settings=settings, opener=opener)

    assert error.value.code is ErrorCode.DIARIZE_INPUT_UNREADABLE


def test_diarize_run_maps_missing_slot_wavs_to_input_unreadable(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, audio_dir = _write_session(settings, "m1", seq_starts={0: 0})
    (audio_dir / "slot-0.wav").unlink()
    assert list(audio_dir.glob("slot-*.wav")) == []
    before = record_path.read_bytes()

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("wav-less run must not transcribe")

    with pytest.raises(FakeYouError, match="no slot wav files") as error:
        diarize_run("m1", settings=settings, opener=opener)
    assert error.value.code is ErrorCode.DIARIZE_INPUT_UNREADABLE
    assert record_path.read_bytes() == before


def test_diarize_run_writes_sidecar_and_keeps_record_identical(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, _ = _write_session(settings, "m1", seq_starts={0: 0, 1: 0})
    before = record_path.read_bytes()

    def opener(request: urllib.request.Request) -> _FakeResponse:
        return _FakeResponse(_turns_payload([(0, 5000, "A"), (6000, 12000, "B")]))

    assert diarize_run("m1", settings=settings, opener=opener) == 0

    assert record_path.read_bytes() == before
    payload = json.loads((record_path.parent / "diarization.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "diarization_correction/v1"
    assert [(turn["start_ms"], turn["end_ms"], turn["speaker"]) for turn in payload["turns"]] == [
        (0, 5000, "A"),
        (6000, 12000, "B"),
    ]
    assert all("corrected_uuid" in turn for turn in payload["turns"])
    assert "discarded words" not in (record_path.parent / "diarization.json").read_text(
        encoding="utf-8"
    )
    assert "discarded words" not in (record_path.parent / "diarization.rttm").read_text(
        encoding="utf-8"
    )


def test_diarize_run_offsets_chunk_turns_by_chunk_start(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.cli.diarize as cli_diarize

    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, _ = _write_session(settings, "m1", seq_starts={0: 0})

    def chunk_wav(seconds: int) -> bytes:
        buffer = BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(CAPTURE_SAMPLE_RATE)
            handle.writeframes(bytes(seconds * CAPTURE_SAMPLE_RATE * 2))
        return buffer.getvalue()

    chunks = [chunk_wav(10), chunk_wav(10)]
    monkeypatch.setattr(cli_diarize, "split_chunks", lambda frames, rate, **kwargs: chunks)
    calls = {"count": 0}

    def opener(request: urllib.request.Request) -> _FakeResponse:
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(_turns_payload([(0, 5000, "A")]))
        return _FakeResponse(_turns_payload([(0, 5000, "B")]))

    assert diarize_run("m1", settings=settings, opener=opener) == 0

    payload = json.loads((record_path.parent / "diarization.json").read_text(encoding="utf-8"))
    assert [(turn["start_ms"], turn["end_ms"], turn["speaker"]) for turn in payload["turns"]] == [
        (0, 5000, "A"),
        (10000, 15000, "B"),
    ]


def test_diarize_run_splits_six_minute_mix_with_five_minute_cap(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.audio.diarize_mix as diarize_mix
    import fakeyou.cli.diarize as cli_diarize

    settings.stt.api_key = SecretStr(SECRET_KEY)
    _write_session(settings, "m1", seq_starts={0: 0})

    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CAPTURE_SAMPLE_RATE)
        handle.writeframes(bytes(6 * 60 * CAPTURE_SAMPLE_RATE * 2))
    monkeypatch.setattr(cli_diarize, "mix_session_slots", lambda audio_dir: buffer.getvalue())

    real_split = diarize_mix.split_chunks
    seen: dict[str, Any] = {}

    def recording(frames: bytes, rate: int, **kwargs: Any) -> list[bytes]:
        seen.update(kwargs)
        return real_split(frames, rate, **kwargs)

    monkeypatch.setattr(cli_diarize, "split_chunks", recording)

    sizes: list[int] = []

    def opener(request: urllib.request.Request) -> _FakeResponse:
        data = request.data
        assert isinstance(data, (bytes, bytearray))
        sizes.append(len(data))
        return _FakeResponse(_turns_payload([]))

    assert diarize_run("m1", settings=settings, opener=opener) == 0

    assert seen.get("max_minutes") == 5.0
    assert len(sizes) == 2
    assert all(size <= 5 * 60 * CAPTURE_SAMPLE_RATE * 2 + 44 + 4096 for size in sizes)


def test_diarize_run_maps_corrupt_slot_wav_to_input_unreadable(settings: Settings) -> None:
    settings.stt.api_key = SecretStr(SECRET_KEY)
    record_path, audio_dir = _write_session(settings, "m1", seq_starts={0: 0})
    (audio_dir / "slot-0.wav").write_bytes(b"not a wav file" * 8)
    before = record_path.read_bytes()

    def opener(request: urllib.request.Request) -> _FakeResponse:
        raise AssertionError("corrupt run must not transcribe")

    with pytest.raises(FakeYouError) as error:
        diarize_run("m1", settings=settings, opener=opener)

    assert error.value.code is ErrorCode.DIARIZE_INPUT_UNREADABLE
    assert isinstance(error.value.__cause__, wave.Error)
    assert record_path.read_bytes() == before


def test_diarize_run_dispatches_meeting_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_run(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "diarize_run", fake_run)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "diarize", "run", "--meeting-id", "m1"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["meeting_id"] == "m1"
    assert set(call) == {"meeting_id", "settings"}


def _show_line(index: int, capture_ms: float) -> str:
    return json.dumps(
        {
            "kind": "transcript",
            "at": "2026-10-08T00:00:00+00:00",
            "id": f"event-{index}",
            "spoken_at": "2026-10-08T00:00:00+00:00",
            "source": "meeting_audio",
            "text": "hello world",
            "participant_id": "unknown",
            "speaker_evidence_id": None,
            "stream_id": "",
            "capture_ms": capture_ms,
        }
    )


def _show_session(settings: Settings, meeting_id: str, captures: list[float]) -> Path:
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        "".join(_show_line(index, capture) + "\n" for index, capture in enumerate(captures)),
        encoding="utf-8",
    )
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "slots.json").write_text(
        json.dumps({"schema": "slot_audio/v1", "slots": {}, "anchors": []}),
        encoding="utf-8",
    )
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: datetime(2026, 10, 8, 12, 0, 0, tzinfo=UTC),
    )
    return record_path


def test_diarize_show_prints_turns_coverage_and_ok_status(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    _show_session(settings, "m1", [1000.0, 2000.0, 20000.0])

    assert diarize_show("m1", settings=settings) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "turns: 1" in lines
    assert "covered: 2/3 transcript entries" in lines
    assert "status: ok" in lines


def test_diarize_show_exits_stale_when_record_changed(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    record_path = _show_session(settings, "m1", [1000.0])
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(_show_line(9, 1100.0) + "\n")

    with pytest.raises(SystemExit) as exit:
        diarize_show("m1", settings=settings)

    assert exit.value.code is ExitCode.INPUT_REJECTED
    assert "stale" in capsys.readouterr().out


def test_diarize_show_reports_invalid_schema_without_stale_label(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    record_path = _show_session(settings, "m1", [1000.0])
    sidecar = record_path.parent / "diarization.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["schema"] = "diarization_correction/v99"
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DiarizeError, match="schema") as error:
        diarize_show("m1", settings=settings)

    assert error.value.code is ErrorCode.DIARIZE_SCHEMA_UNSUPPORTED
    assert "stale" not in capsys.readouterr().out


def test_diarize_show_reports_invalid_turns_without_stale_label(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    record_path = _show_session(settings, "m1", [1000.0])
    sidecar = record_path.parent / "diarization.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["turns"] = [{"start_ms": "zero", "end_ms": 1000, "corrected_uuid": "u"}]
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DiarizeError, match="turns are invalid") as error:
        diarize_show("m1", settings=settings)

    assert error.value.code is ErrorCode.DIARIZE_TURNS_INVALID
    assert "stale" not in capsys.readouterr().out


def test_diarize_show_refuses_missing_sidecar(settings: Settings) -> None:
    record_path = meeting_record_path(settings.paths.record_dir, "m1")
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(_show_line(0, 1000.0) + "\n", encoding="utf-8")

    with pytest.raises(FakeYouError, match="no diarization correction") as error:
        diarize_show("m1", settings=settings)

    assert error.value.code is ErrorCode.DIARIZE_CORRECTION_MISSING


def test_diarize_show_dispatches_meeting_id(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_show(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, "diarize_show", fake_show)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "diarize", "show", "--meeting-id", "m1"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert call["meeting_id"] == "m1"
    assert set(call) == {"meeting_id", "settings"}
