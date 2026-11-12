from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Literal

from fakeyou.perception.frames import compare_screens
from fakeyou.perception.percepts import Keyframe


def observations_match(left: Keyframe, right: Keyframe) -> bool:
    return compare_screens(left, right) in {"same", "minor"}


@dataclass(frozen=True, slots=True)
class CameraEvidence:
    answer: str
    resolution: Literal["cache_hit", "fresh"]
    age_ms: int
    vision_latency_ms: int
    sequence: int = 0
    generation: int = 0


@dataclass(slots=True)
class CameraCacheEntry:
    question: str
    answer: str
    keyframe: Keyframe
    stored_at: float
    sequence: int
    generation: int
    stream_id: str


class StaleEvidence(Exception):
    pass


@dataclass(slots=True)
class CameraCache:
    entries: dict[str, CameraCacheEntry] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock_for(self, participant_id: str) -> asyncio.Lock:
        lock = self.locks.get(participant_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[participant_id] = lock
        return lock

    def get(self, participant_id: str) -> CameraCacheEntry | None:
        return self.entries.get(participant_id)

    def put(self, participant_id: str, entry: CameraCacheEntry) -> None:
        self.entries[participant_id] = entry

    def drop(self, participant_id: str) -> None:
        self.entries.pop(participant_id, None)

    def clear(self) -> None:
        self.entries.clear()
