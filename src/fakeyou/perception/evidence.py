from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fakeyou.foundation.publication import EvidenceSource as EvidenceSource
from fakeyou.foundation.publication import SnapshotRef as SnapshotRef
from fakeyou.perception.inspector import VisualInspection

Acquisition = Literal["not_attempted", "reused", "inference"]
EvidenceStatus = Literal["produced", "insufficient", "unavailable"]
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
class ProviderAttempt:
    attempt_id: str
    provider: str | None
    response_id: str | None
    finished_at: float


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    source: EvidenceSource
    subject_id: str
    snapshot: SnapshotRef | None
    acquisition: Acquisition
    status: EvidenceStatus
    verification: Verification
    strategy: EvidenceStrategy | None
    code: EvidenceCode
    answer: str | None
    observed_at: datetime
    age_ms: int = 0
    vision_latency_ms: int = 0
    error_type: str | None = None
    subject_label: str | None = None
    attempt: ProviderAttempt | None = None


def inspection_outcome(inspection: VisualInspection) -> tuple[EvidenceStatus, EvidenceCode]:
    if inspection.verdict == "supported":
        return ("produced", "ok")
    return ("insufficient", "insufficient_evidence")


def evidence_resolution(result: EvidenceResult) -> str:
    if result.status == "unavailable":
        return "unavailable"
    return "cache_hit" if result.acquisition == "reused" else "fresh"


def evidence_payload(result: EvidenceResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": result.code,
        "resolution": evidence_resolution(result),
        "status": result.status,
        "acquisition": result.acquisition,
        "age_ms": result.age_ms,
        "vision_latency_ms": result.vision_latency_ms,
    }
    if result.answer is not None:
        payload["answer"] = result.answer
    if result.error_type is not None:
        payload["error_type"] = result.error_type
    if result.source == "camera":
        payload["participant"] = result.subject_id
    else:
        payload["screen"] = result.subject_id
    if result.subject_label is not None:
        payload["title"] = result.subject_label
    if result.strategy is not None:
        payload["strategy"] = result.strategy
    return payload
