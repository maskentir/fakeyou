import wave
from io import BytesIO
from pathlib import Path

from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.pcm import PCM16_SAMPLE_BYTES, decode_pcm16, encode_pcm16

_BLOCK_SAMPLES = 960000
_CHUNK_MINUTES = 10
_CHUNK_BYTES = 33554432
_HEADER_BYTES = 44
_MAX_SAMPLE = 32767
_MIN_SAMPLE = -32768


def _read_slot_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if (
            handle.getnchannels() != 1
            or handle.getsampwidth() != PCM16_SAMPLE_BYTES
            or handle.getframerate() != CAPTURE_SAMPLE_RATE
        ):
            raise ValueError(f"unsupported wav format in {path.name}")
        return handle.readframes(handle.getnframes())


def _write_wav(payload: bytes, frames_per_second: int) -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(PCM16_SAMPLE_BYTES)
        handle.setframerate(frames_per_second)
        handle.writeframes(payload)
    return buffer.getvalue()


def mix_session_slots(audio_dir: Path) -> bytes:
    paths = sorted(audio_dir.glob("slot-*.wav"))
    if not paths:
        raise FileNotFoundError(f"no slot wav files in {audio_dir}")
    tracks = [_read_slot_wav(path) for path in paths]
    counts = [len(track) // PCM16_SAMPLE_BYTES for track in tracks]
    longest = max(counts)
    totals: list[int] = []
    for start in range(0, longest, _BLOCK_SAMPLES):
        stop = min(start + _BLOCK_SAMPLES, longest)
        blocks: list[tuple[int, ...]] = []
        for track, count in zip(tracks, counts, strict=True):
            low = min(start, count)
            high = min(stop, count)
            if high > low:
                blocks.append(
                    decode_pcm16(track[low * PCM16_SAMPLE_BYTES : high * PCM16_SAMPLE_BYTES])
                )
            else:
                blocks.append(())
        bases = [min(start, count) for count in counts]
        for offset in range(stop - start):
            index = start + offset
            total = 0
            for block, base in zip(blocks, bases, strict=True):
                slot_index = index - base
                if slot_index < len(block):
                    total += block[slot_index]
            if total > _MAX_SAMPLE:
                total = _MAX_SAMPLE
            elif total < _MIN_SAMPLE:
                total = _MIN_SAMPLE
            totals.append(total)
    return _write_wav(encode_pcm16(totals), CAPTURE_SAMPLE_RATE)


def split_chunks(
    frames: bytes,
    frames_per_second: int,
    *,
    max_minutes: float = _CHUNK_MINUTES,
    max_bytes: int = _CHUNK_BYTES,
) -> list[bytes]:
    if frames_per_second <= 0:
        raise ValueError("frames per second must be positive")
    if len(frames) % 2:
        raise ValueError("frames length must be even")
    if max_minutes <= 0:
        raise ValueError("max minutes must be positive")
    if max_bytes <= _HEADER_BYTES:
        raise ValueError("max bytes must exceed header size")
    limit = min(
        int(max_minutes * 60 * frames_per_second * PCM16_SAMPLE_BYTES), max_bytes - _HEADER_BYTES
    )
    limit = limit // PCM16_SAMPLE_BYTES * PCM16_SAMPLE_BYTES
    if limit <= 0:
        raise ValueError("chunk size limit must be positive")
    return [
        _write_wav(frames[start : start + limit], frames_per_second)
        for start in range(0, len(frames), limit)
    ]
