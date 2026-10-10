import asyncio
import json
import os
import wave
from pathlib import Path

import pytest
from pipecat.transports.base_transport import TransportParams
from pydantic import ValidationError

from fakeyou.audio.stream_capture import StreamAudioInput, StreamChunk, StreamRegistry
from fakeyou.config.settings import AudioSettings, Configuration
from fakeyou.observability.journal import Journal
from fakeyou.persistence.audio_persist import AudioRecordingSummary, SlotAudioWriter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        self.now += 0.5
        return self.now


def _pcm(byte: int, frames: int) -> bytes:
    return bytes([byte]) * (frames * 2)


def _writer(directory: Path, clock: FakeClock, lines: list[str]) -> SlotAudioWriter:
    journal = Journal("meeting-1", emit=lines.append, echo=False)
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

    with pytest.raises(ValueError, match="writer is closed"):
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


class _SilentInput(StreamAudioInput):
    def __init__(self) -> None:
        super().__init__(TransportParams(audio_in_enabled=True))
        self.fed: list[bytes] = []

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        self.fed.append(audio)


def _wiring_chunk(slot: int, seq: int) -> StreamChunk:
    return StreamChunk(
        slot=slot,
        seq=seq,
        csrcs=(7,),
        level=0.5,
        page_ms=1000.0 + seq * 20.0,
        pcm=_pcm(7, 80),
    )


async def test_registry_with_recorder_writes_wav_sidecar_and_events(tmp_path: Path) -> None:
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append, echo=False)
    directory = tmp_path / "meeting-1" / "audio"
    writer = SlotAudioWriter(directory, monotonic=FakeClock().monotonic, journal=journal)
    registry = StreamRegistry(input_factory=_SilentInput, journal=journal, recorder=writer)
    for seq in (0, 1, 3):
        await registry.feed(_wiring_chunk(0, seq))
    summary = writer.close()

    assert summary == AudioRecordingSummary(
        slots=(0,), seconds_total=0.02, bytes_total=640, gaps_filled=1, chunks=3
    )
    with wave.open(str(directory / "slot-0.wav"), "rb") as handle:
        assert handle.getnframes() == 320
    sidecar = json.loads((directory / "slots.json").read_text())
    assert sidecar["slots"]["0"]["gaps_filled"] == 1
    assert len(_rows(lines, "audio_recording_started")) == 1
    assert len(_rows(lines, "audio_recording_finished")) == 1


async def test_registry_without_recorder_writes_no_audio_files_or_events(
    tmp_path: Path,
) -> None:
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append, echo=False)
    registry = StreamRegistry(input_factory=_SilentInput, journal=journal)
    for seq in (0, 1, 3):
        await registry.feed(_wiring_chunk(0, seq))

    assert registry.sessions[0].seq == 3
    assert await asyncio.to_thread(os.listdir, tmp_path) == []
    events = [json.loads(line)["event"] for line in lines]
    assert "audio_recording_started" not in events
    assert "audio_recording_finished" not in events


async def test_registry_write_failure_records_fault_and_keeps_session(
    tmp_path: Path,
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("read-only directory is writable for root")
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append, echo=False)
    directory = tmp_path / "audio"
    writer = SlotAudioWriter(directory, monotonic=FakeClock().monotonic, journal=journal)
    registry = StreamRegistry(input_factory=_SilentInput, journal=journal, recorder=writer)
    directory.chmod(0o555)
    try:
        await registry.feed(_wiring_chunk(0, 0))
    finally:
        directory.chmod(0o755)

    assert registry.sessions[0].seq == 0
    assert registry.sessions[0].level == 0.5
    faults = _rows(lines, "fault_recorded")
    assert len(faults) == 1
    assert faults[0]["source"] == "audio"
    assert str(faults[0]["detail"]).startswith("audio_persist")
