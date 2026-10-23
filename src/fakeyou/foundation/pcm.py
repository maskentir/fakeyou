import struct
from collections.abc import Sequence
from typing import NamedTuple

import numpy as np

PCM16_SAMPLE_BYTES = 2
PCM16_FULL_SCALE = 32768.0
VOICED_RMS = 200.0
VOICED_FRAME_SECONDS = 0.02
VOICED_PAD_FRAMES = 2


def decode_pcm16(pcm: bytes) -> tuple[int, ...]:
    count = len(pcm) // PCM16_SAMPLE_BYTES
    return struct.unpack(f"<{count}h", pcm[: count * PCM16_SAMPLE_BYTES])


def encode_pcm16(samples: Sequence[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def pcm16_to_floats(pcm: bytes) -> list[float]:
    count = len(pcm) // PCM16_SAMPLE_BYTES
    samples = np.frombuffer(pcm[: count * PCM16_SAMPLE_BYTES], dtype="<i2")
    scaled: list[float] = (samples.astype(np.float64) / PCM16_FULL_SCALE).tolist()
    return scaled


class VoicedClip(NamedTuple):
    pcm: bytes
    longest_pause_seconds: float


def voiced_clip(pcm: bytes, rate: int) -> VoicedClip | None:
    frame = int(rate * VOICED_FRAME_SECONDS)
    count = len(pcm) // PCM16_SAMPLE_BYTES // frame
    if count == 0:
        return None
    samples = np.frombuffer(pcm[: count * frame * PCM16_SAMPLE_BYTES], dtype="<i2")
    energy = np.sqrt((samples.astype(np.float64).reshape(count, frame) ** 2).mean(axis=1))
    voiced = np.flatnonzero(energy >= VOICED_RMS)
    if voiced.size == 0:
        return None
    first = max(0, int(voiced[0]) - VOICED_PAD_FRAMES)
    last = min(count, int(voiced[-1]) + 1 + VOICED_PAD_FRAMES)
    pause_frames = int(np.diff(voiced).max()) - 1 if voiced.size > 1 else 0
    frame_bytes = frame * PCM16_SAMPLE_BYTES
    return VoicedClip(
        pcm[first * frame_bytes : last * frame_bytes], pause_frames * VOICED_FRAME_SECONDS
    )
