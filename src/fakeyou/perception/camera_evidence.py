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
    generation: int = 0


@dataclass(slots=True)
class CameraCacheEntry:
    question: str
    answer: str
    keyframe: Keyframe
    stored_at: float


@dataclass(frozen=True, slots=True)
class Generation:
    epoch: int
    participant: int


class StaleEvidence(Exception):
    pass


@dataclass(slots=True)
class CameraCache:
    entries: dict[str, CameraCacheEntry] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    epoch: int = 0
    participants: dict[str, int] = field(default_factory=dict)

    def lock_for(self, participant_id: str) -> asyncio.Lock:
        lock = self.locks.get(participant_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[participant_id] = lock
        return lock

    def generation(self, participant_id: str) -> Generation:
        return Generation(self.epoch, self.participants.get(participant_id, 0))

    def get(self, participant_id: str, question: str) -> CameraCacheEntry | None:
        entry = self.entries.get(participant_id)
        if entry is None or entry.question != question:
            return None
        return entry

    def put(self, participant_id: str, entry: CameraCacheEntry) -> None:
        self.entries[participant_id] = entry

    def drop(self, participant_id: str) -> None:
        self.participants[participant_id] = self.participants.get(participant_id, 0) + 1
        self.entries.pop(participant_id, None)

    def clear(self) -> None:
        self.epoch += 1
        self.entries.clear()
