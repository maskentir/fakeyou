import json
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from fakeyou.audio_persist import AudioRecordingSummary, SlotAudioWriter
from fakeyou.journal import Journal
from fakeyou.settings import AudioSettings, Configuration


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        self.now += 0.5
        return self.now


def _pcm(byte: int, frames: int) -> bytes:
    return bytes([byte]) * (frames * 2)


def _writer(directory: Path, clock: FakeClock, lines: list[str]) -> SlotAudioWriter:
    journal = Journal("meeting-1", emit=lines.append, stdout=False)
    return SlotAudioWriter(directory, monotonic=clock.monotonic, journal=journal)


def _rows(lines: list[str], event: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in lines if json.loads(line)["event"] == event]


def test_sequential_chunks_produce_readable_wav(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    for seq in range(3):
        writer.write_chunk(0, seq, 1000.0 + seq * 20.0, _pcm(7, 80))
    summary = writer.close()

    assert summary == AudioRecordingSummary(
        slots=(0,), seconds_total=0.015, bytes_total=480, gaps_filled=0, chunks=3
    )
    with wave.open(str(tmp_path / "audio" / "slot-0.wav"), "rb") as handle:
        assert handle.getframerate() == 16000
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getnframes() == 240
    sidecar = json.loads((tmp_path / "audio" / "slots.json").read_text())
    assert sidecar["schema"] == "slot_audio/v1"
    assert sidecar["slots"] == {
        "0": {"seq_start": 0, "seq_end": 2, "frames": 240, "gaps_filled": 0}
    }
    assert sidecar["anchors"] == [
        {"slot": 0, "seq": seq, "page_ms": 1000.0 + seq * 20.0, "mono": 1000.5 + seq * 0.5}
        for seq in range(3)
    ]


def test_sequence_gap_is_zero_filled(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    writer.write_chunk(0, 5, 1000.0, _pcm(9, 80))
    writer.write_chunk(0, 8, 1060.0, _pcm(9, 80))
    summary = writer.close()

    assert summary.gaps_filled == 1
    assert summary.bytes_total == 640
    with wave.open(str(tmp_path / "audio" / "slot-0.wav"), "rb") as handle:
        assert handle.getnframes() == 320
        data = handle.readframes(320)
    assert data[:160] == _pcm(9, 80)
    assert data[160:480] == bytes(320)
    assert data[480:] == _pcm(9, 80)
    sidecar = json.loads((tmp_path / "audio" / "slots.json").read_text())
    assert sidecar["slots"]["0"]["gaps_filled"] == 1
    assert sidecar["slots"]["0"]["seq_start"] == 5
    assert sidecar["slots"]["0"]["seq_end"] == 8


def test_duplicate_and_old_chunks_are_dropped(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    writer.write_chunk(0, 0, 1000.0, _pcm(5, 80))
    writer.write_chunk(0, 1, 1020.0, _pcm(5, 80))
    writer.write_chunk(0, 1, 1020.0, _pcm(6, 80))
    writer.write_chunk(0, 0, 1000.0, _pcm(6, 80))
    summary = writer.close()

    assert summary.chunks == 2
    assert summary.bytes_total == 320
    with wave.open(str(tmp_path / "audio" / "slot-0.wav"), "rb") as handle:
        assert handle.getnframes() == 160
        assert handle.readframes(160) == _pcm(5, 80) + _pcm(5, 80)
    sidecar = json.loads((tmp_path / "audio" / "slots.json").read_text())
    assert len(sidecar["anchors"]) == 2


def test_two_slots_produce_two_wavs_and_shared_sidecar(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    writer.write_chunk(0, 0, 1000.0, _pcm(3, 80))
    writer.write_chunk(1, 0, 1005.0, _pcm(4, 40))
    writer.write_chunk(0, 1, 1020.0, _pcm(3, 80))
    writer.write_chunk(1, 1, 1025.0, _pcm(4, 40))
    writer.write_chunk(1, 2, 1045.0, _pcm(4, 40))
    summary = writer.close()

    assert summary.slots == (0, 1)
    assert (tmp_path / "audio" / "slot-0.wav").exists()
    assert (tmp_path / "audio" / "slot-1.wav").exists()
    with wave.open(str(tmp_path / "audio" / "slot-0.wav"), "rb") as handle:
        assert handle.getnframes() == 160
    with wave.open(str(tmp_path / "audio" / "slot-1.wav"), "rb") as handle:
        assert handle.getnframes() == 120
    sidecar = json.loads((tmp_path / "audio" / "slots.json").read_text())
    assert set(sidecar["slots"]) == {"0", "1"}
    assert sidecar["slots"]["1"] == {
        "seq_start": 0,
        "seq_end": 2,
        "frames": 120,
        "gaps_filled": 0,
    }
    assert len(sidecar["anchors"]) == 5
    assert {tuple(sorted(anchor)) for anchor in sidecar["anchors"]} == {
        ("mono", "page_ms", "seq", "slot")
    }


def test_close_is_idempotent_and_journals_single_finished_event(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    for seq in range(3):
        writer.write_chunk(0, seq, 1000.0 + seq * 20.0, _pcm(7, 80))
    writer.write_chunk(1, 0, 1000.0, _pcm(7, 80))
    writer.write_chunk(1, 2, 1040.0, _pcm(7, 80))
    first = writer.close()
    second = writer.close()

    assert first == second
    assert first == AudioRecordingSummary(
        slots=(0, 1), seconds_total=0.03, bytes_total=960, gaps_filled=1, chunks=5
    )
    finished = _rows(lines, "audio_recording_finished")
    assert len(finished) == 1
    assert finished[0]["slots"] == [0, 1]
    assert finished[0]["seconds_total"] == 0.03
    assert finished[0]["bytes_total"] == 960
    assert finished[0]["gaps_filled"] == 1
    assert finished[0]["chunks"] == 5


def test_started_event_carries_basename_only(tmp_path: Path) -> None:
    lines: list[str] = []
    directory = tmp_path / "meeting-7" / "audio"
    writer = _writer(directory, FakeClock(), lines)
    writer.close()

    [started] = _rows(lines, "audio_recording_started")
    assert started["directory"] == "audio"
    assert "/" not in str(started["directory"])


def test_write_after_close_raises(tmp_path: Path) -> None:
    lines: list[str] = []
    writer = _writer(tmp_path / "audio", FakeClock(), lines)
    writer.close()

    with pytest.raises(ValueError):
        writer.write_chunk(0, 0, 1000.0, _pcm(7, 80))


def test_audio_settings_default_to_disabled_with_thirty_day_retention() -> None:
    settings = AudioSettings()

    assert settings.persist_enabled is False
    assert settings.retention_days == 30
    assert Configuration().audio.persist_enabled is False
    assert Configuration().audio.retention_days == 30


def test_audio_settings_reject_non_positive_retention() -> None:
    with pytest.raises(ValidationError):
        AudioSettings(retention_days=0)
