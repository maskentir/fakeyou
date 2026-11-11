from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from fakeyou.perception.frames import compare_screens
from fakeyou.perception.percepts import Keyframe


def observations_match(left: Keyframe, right: Keyframe) -> bool:
    return compare_screens(left, right) in {"same", "minor"}


class CameraObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_id: str
    observed_at: datetime
    clothing: str = Field(description="What the person is wearing, or an empty string.")
    visible_objects: list[str] = Field(default_factory=list)
    background: str = Field(description="The room or place behind the person, or an empty string.")
    appearance: str = Field(description="How the person looks, or an empty string.")

    def as_evidence(self) -> str:
        parts = [
            f"clothing: {self.clothing}",
            f"appearance: {self.appearance}",
            f"background: {self.background}",
            f"visible objects: {', '.join(self.visible_objects)}",
        ]
        return "Observed from the live camera: " + "; ".join(parts) + "."


@dataclass(frozen=True, slots=True)
class CameraEvidence:
    observation: CameraObservation
    resolution: Literal["cache_hit", "fresh"]
    age_ms: int
    vision_latency_ms: int


@dataclass(slots=True)
class CameraCacheEntry:
    observation: CameraObservation
    keyframe: Keyframe
    stored_at: float


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
