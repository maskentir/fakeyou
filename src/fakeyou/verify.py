from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fakeyou.faults import Fault

if TYPE_CHECKING:
    from fakeyou.faults import FaultReporter
    from fakeyou.identity import SpeakerRegistry, SpeakerRow
    from fakeyou.journal import Journal
    from fakeyou.voiceprint_embed import SpeakerEmbedder
    from fakeyou.voiceprint_match import VoiceScores


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    theta_llr: float
    margin_floor: float
    snorm_cohort: tuple[float, ...]
    platt_a: float
    platt_b: float
    platt_version: str
    report_id: str


@dataclass(frozen=True, slots=True)
class VerifiedDecision:
    uuid: str
    person_id: str
    name: str
    llr: float
    margin: float


def _artifact_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _artifact_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value


def load_calibration_artifact(path: str | Path) -> CalibrationArtifact | None:
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    theta = _artifact_number(raw.get("theta_llr"))
    floor = _artifact_number(raw.get("margin_floor"))
    slope = _artifact_number(raw.get("platt_a"))
    bias = _artifact_number(raw.get("platt_b"))
    version = _artifact_text(raw.get("platt_version"))
    report = _artifact_text(raw.get("report_id"))
    cohort_raw = raw.get("snorm_cohort")
    if (
        theta is None
        or floor is None
        or slope is None
        or bias is None
        or version is None
        or report is None
        or not isinstance(cohort_raw, list)
        or not cohort_raw
    ):
        return None
    cohort: list[float] = []
    for entry in cohort_raw:
        number = _artifact_number(entry)
        if number is None:
            return None
        cohort.append(number)
    return CalibrationArtifact(
        theta_llr=theta,
        margin_floor=floor,
        snorm_cohort=tuple(cohort),
        platt_a=slope,
        platt_b=bias,
        platt_version=version,
        report_id=report,
    )


def claim_of(row: SpeakerRow) -> str | None:
    return row.display_claim


def decide_verified(
    uuid: str,
    scores: VoiceScores,
    claim: str | None,
    artifact: CalibrationArtifact | None,
    *,
    registry: SpeakerRegistry | None = None,
    slot: int | None = None,
    overlap: bool | None = None,
    embedder: SpeakerEmbedder | None = None,
    journal: Journal | None = None,
    faults: FaultReporter | None = None,
) -> VerifiedDecision | None:
    if artifact is None or registry is None or journal is None:
        return None
    if embedder is None or slot is None:
        return None
    model_id = embedder.model_id
    if not model_id:
        return None
    person = scores.person_id
    if person is None or scores.outcome == "negative":
        return None
    overlapped = overlap if overlap is not None else len(registry.stream_csrcs(slot)) > 1
    if overlapped:
        return None
    cohort = artifact.snorm_cohort
    if not cohort:
        return None
    mean = statistics.mean(cohort)
    std = statistics.pstdev(cohort)
    if std <= 0.0:
        return None
    llr = artifact.platt_a * (scores.s1 - mean) / std + artifact.platt_b
    if llr < artifact.theta_llr:
        return None
    margin = scores.s1 - scores.s2
    if margin < artifact.margin_floor:
        return None
    owner = scores.owner_name
    consent = scores.consent_ts
    if not owner or not consent:
        return None
    if claim is None:
        return None
    if claim != owner:
        if faults is not None:
            faults.record(
                Fault(
                    source="audio",
                    kind="voiceprint_misbind",
                    detail=f"claim {claim} disagrees with voice owner {owner} for speaker {uuid}",
                )
            )
        return None
    bound = registry.note_verified(uuid, person, claim)
    if bound == "conflict":
        if faults is not None:
            faults.record(
                Fault(
                    source="audio",
                    kind="voiceprint_misbind",
                    detail=f"voice {person} conflicts with verified binding for speaker {uuid}",
                )
            )
        return None
    if bound == "unknown":
        return None
    decision = VerifiedDecision(uuid=uuid, person_id=person, name=claim, llr=llr, margin=margin)
    if bound == "repeat":
        return decision
    journal.record(
        "name_verified",
        source="audio",
        enrollment_id=person,
        model_version=model_id,
        score_raw=round(scores.s1, 4),
        snorm_mean=round(mean, 4),
        snorm_std=round(std, 4),
        snorm_n=len(cohort),
        platt_version=artifact.platt_version,
        theta_llr=artifact.theta_llr,
        report_id=artifact.report_id,
        claim_agree=True,
        uuid=uuid,
        slot=slot,
        llr=round(llr, 4),
        margin=round(margin, 4),
    )
    return decision
