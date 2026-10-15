import struct
from collections.abc import Sequence

import numpy as np

PCM16_SAMPLE_BYTES = 2
PCM16_FULL_SCALE = 32768.0


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
