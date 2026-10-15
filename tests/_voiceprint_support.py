from __future__ import annotations

import math
import random
import struct
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

from fakeyou.db.voiceprint_store import VoiceprintRecord
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder
from tests._voiceprint_fakes import InMemoryVoiceprints

CONSENT_AT = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)
ENROLLED_AT = datetime(2026, 10, 7, 10, 0, 1, tzinfo=UTC)
LAST_USED_AT = datetime(2026, 10, 7, 10, 0, 2, tzinfo=UTC)
CHALLENGE_ID = "11111111-2222-3333-4444-555555555555"
CHALLENGE_DIGITS = "12345678"


class ScriptedEmbedder(SpeakerEmbedder):
    def __init__(self, vectors: list[tuple[float, ...]]) -> None:
        self.vectors = list(vectors)
        self.calls: list[list[tuple[float, ...]]] = []

    @override
    def embed(self, utterances: Any) -> tuple[float, ...]:
        self.calls.append([tuple(utterance) for utterance in utterances])
        return self.vectors.pop(0)


def saved(store: InMemoryVoiceprints, person_id: str) -> VoiceprintRecord:
    [record] = [row for row in store.load_all() if row.person_id == person_id]
    return record


def write_tone_clip(
    path: Path,
    *,
    rate: int = 16000,
    seconds: float = 3.0,
    freq: float = 440.0,
    amplitude: float = 0.5,
) -> Path:
    frames = bytearray()
    total = int(rate * seconds)
    for index in range(total):
        sample = int(amplitude * 32767.0 * math.sin(2.0 * math.pi * freq * index / rate))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return path


def write_speechlike_clip(path: Path, *, seconds: float = 10.0, noise: float = 0.0) -> Path:
    rng = random.Random(7)
    frames = bytearray()
    total = int(16000 * seconds)
    for index in range(total):
        voiced = (index % 12000) < 8000
        tone = 0.5 * math.sin(2.0 * math.pi * 440.0 * index / 16000) if voiced else 0.0
        sample = int(32767.0 * max(-1.0, min(1.0, tone + rng.uniform(-noise, noise))))
        frames += struct.pack("<h", sample)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(frames))
    return path


def stored_record(
    person_id: str,
    embedding: tuple[float, ...],
    *,
    is_negative: bool = False,
    last_used_at: datetime = LAST_USED_AT,
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=CONSENT_AT,
        policy_version="v1",
        is_negative=is_negative,
        enrolled_at=ENROLLED_AT,
        last_used_at=last_used_at,
    )
