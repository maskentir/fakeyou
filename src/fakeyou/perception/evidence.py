from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EvidenceSource = Literal["camera", "screen"]
Acquisition = Literal["reused", "inference"]
EvidenceStatus = Literal["supported", "insufficient", "unavailable"]
Verification = Literal["unverified", "verified"]
EvidenceStrategy = Literal["targeted_vision", "screen_reading"]
EvidenceCode = Literal[
    "ok",
    "insufficient_evidence",
    "camera_unavailable",
    "budget_exhausted",
    "look_failed",
    "unknown_screen",
    "not_meeting_content",
    "frame_unavailable",
    "unknown_participant",
]


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    source: EvidenceSource
    subject_id: str
    stream_id: str
    epoch: int
    sequence: int
    generation: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    source: EvidenceSource
    subject_id: str
    snapshot: SnapshotRef | None
    acquisition: Acquisition
    status: EvidenceStatus
    verification: Verification
    strategy: EvidenceStrategy
    code: EvidenceCode
    answer: str | None
    observed_at: datetime
    age_ms: int = 0
    vision_latency_ms: int = 0
    error_type: str | None = None
