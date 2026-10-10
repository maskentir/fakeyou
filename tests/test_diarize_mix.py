import math
import struct
import wave
from io import BytesIO
from pathlib import Path

import pytest

from fakeyou.audio.diarize_mix import mix_session_slots, split_chunks


def _write_slot_wav(path: Path, samples: list[int]) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _read_samples(payload: bytes) -> list[int]:
    with wave.open(BytesIO(payload), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        count = handle.getnframes()
        data = handle.readframes(count)
    return list(struct.unpack(f"<{count}h", data)) if count else []


def _tone(freq: float, count: int) -> list[int]:
    return [int(10000 * math.sin(2.0 * math.pi * freq * i / 16000.0)) for i in range(count)]


def _clamp(value: int) -> int:
    return max(-32768, min(32767, value))


def _read_chunk_payload(chunk: bytes, expected_rate: int = 16000) -> bytes:
    with wave.open(BytesIO(chunk), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == expected_rate
        return handle.readframes(handle.getnframes())


def test_mix_two_tones_sums_clamps_and_zero_fills_short_slot(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    first = _tone(440.0, 160)
    second = _tone(660.0, 240)
    first[0] = 30000
    first[1] = -30000
    second[0] = 30000
    second[1] = -30000
    _write_slot_wav(audio_dir / "slot-0.wav", first)
    _write_slot_wav(audio_dir / "slot-1.wav", second)

    mixed = _read_samples(mix_session_slots(audio_dir))

    assert len(mixed) == 240
    assert mixed[0] == 32767
    assert mixed[1] == -32768
    assert (
        mixed == [_clamp(a + b) for a, b in zip(first, second, strict=False)] + second[len(first) :]
    )


def test_split_25_minutes_yields_10_10_5_chunks_within_size_limit() -> None:
    frames = bytes(range(256)) * (25 * 60 * 16000 * 2 // 256)

    chunks = split_chunks(frames, 16000)

    assert len(chunks) == 3
    assert [len(chunk) - 44 for chunk in chunks] == [
        10 * 60 * 16000 * 2,
        10 * 60 * 16000 * 2,
        5 * 60 * 16000 * 2,
    ]
    assert all(len(chunk) <= 32 * 1024 * 1024 for chunk in chunks)
    payload = b"".join(_read_chunk_payload(chunk) for chunk in chunks)
    assert payload == frames


def test_split_bytes_cap_binds_before_time_cap_at_high_rate() -> None:
    rate = 48000
    limit = 32 * 1024 * 1024 - 44
    total = limit + 5000
    assert total < 10 * 60 * rate * 2
    frames = bytes(range(256)) * (total // 256) + bytes(range(total % 256))

    chunks = split_chunks(frames, rate)

    assert len(chunks) == 2
    assert [len(chunk) - 44 for chunk in chunks] == [limit, 5000]
    assert all(len(chunk) <= 32 * 1024 * 1024 for chunk in chunks)
    payload = b"".join(_read_chunk_payload(chunk, rate) for chunk in chunks)
    assert payload == frames


def test_split_custom_five_minute_cap_splits_12_minutes_into_3_chunks() -> None:
    frames = bytes(range(256)) * (12 * 60 * 16000 * 2 // 256)

    chunks = split_chunks(frames, 16000, max_minutes=5.0)

    assert len(chunks) == 3
    assert [len(chunk) - 44 for chunk in chunks] == [
        5 * 60 * 16000 * 2,
        5 * 60 * 16000 * 2,
        2 * 60 * 16000 * 2,
    ]
    payload = b"".join(_read_chunk_payload(chunk) for chunk in chunks)
    assert payload == frames


def test_split_with_odd_length_frames_raises() -> None:
    with pytest.raises(ValueError):
        split_chunks(bytes(33), 16000)


def test_mix_without_slot_wavs_raises(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()

    with pytest.raises(FileNotFoundError):
        mix_session_slots(audio_dir)


def test_mix_with_wrong_format_raises(tmp_path: Path) -> None:
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    with wave.open(str(audio_dir / "slot-0.wav"), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(bytes(16))

    with pytest.raises(ValueError):
        mix_session_slots(audio_dir)


def test_split_empty_frames_yields_no_chunks() -> None:
    assert split_chunks(b"", 16000) == []


def test_split_with_non_positive_rate_raises() -> None:
    with pytest.raises(ValueError):
        split_chunks(bytes(32), 0)
