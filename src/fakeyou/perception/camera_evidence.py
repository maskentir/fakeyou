from __future__ import annotations

import asyncio
import re
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

    def drop(self, participant_id: str) -> None:
        self.entries.pop(participant_id, None)

    def clear(self) -> None:
        self.entries.clear()


SchemaField = Literal["clothing", "appearance", "background", "visible_objects"]
Strategy = Literal["structured_observation", "targeted_vision"]
Sufficiency = Literal["sufficient", "insufficient"]

CLOTHING = re.compile(
    r"\b(shirt|t[- ]?shirt|kaos|baju|jacket|jaket|hoodie|sweater|dress|wearing|"
    r"pakaian|outfit|clothes|clothing|top|blouse)\b",
    re.I,
)
BACKGROUND = re.compile(
    r"\b(background|behind|room|ruangan|latar|tempat|where are|di mana|dimana)\b",
    re.I,
)
APPEARANCE = re.compile(
    r"\b(look like|appearance|terlihat seperti|how .* look|rambut|hair|glasses|"
    r"kacamata|beard|jenggot)\b",
    re.I,
)
OBJECTS = re.compile(
    r"\b(holding|memegang|object|benda|barang|using|memakai|carrying|membawa)\b",
    re.I,
)
CHANGE = re.compile(
    r"\b(changed|berubah|perubahan|different from|sejak tadi|since earlier|dibanding|"
    r"sebelumnya|before)\b",
    re.I,
)
DETAIL = re.compile(
    r"\b(text|tulisan|written|write|kata|word|logo|number|nomor|angka|berapa|"
    r"how many|count|jari|finger|gesture|tunjuk|pointing|showing|holding up|angkat)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class CameraQuestionNeed:
    strategy: Strategy
    field: SchemaField | None = None
    reason: str = "general"


def question_need(question: str) -> CameraQuestionNeed:
    if CHANGE.search(question):
        return CameraQuestionNeed("structured_observation", None, "change")
    if DETAIL.search(question):
        return CameraQuestionNeed("targeted_vision", None, "detail")
    candidates: tuple[tuple[SchemaField, re.Pattern[str]], ...] = (
        ("clothing", CLOTHING),
        ("background", BACKGROUND),
        ("appearance", APPEARANCE),
        ("visible_objects", OBJECTS),
    )
    matches = [field for field, pattern in candidates if pattern.search(question)]
    if len(matches) == 1:
        return CameraQuestionNeed("structured_observation", matches[0], "schema")
    if matches:
        return CameraQuestionNeed("targeted_vision", None, "ambiguous")
    return CameraQuestionNeed("targeted_vision", None, "general")


def observation_sufficient(observation: CameraObservation, need: CameraQuestionNeed) -> bool:
    if need.reason == "change":
        return False
    field = need.field
    if field == "clothing":
        return bool(observation.clothing.strip())
    if field == "appearance":
        return bool(observation.appearance.strip())
    if field == "background":
        return bool(observation.background.strip())
    if field == "visible_objects":
        return bool(observation.visible_objects)
    return False
