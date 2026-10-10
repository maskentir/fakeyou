from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fakeyou.observability.diagnostics import DEFAULT_DIAGNOSTICS, Diagnostics
from fakeyou.observability.faults import Fault

if TYPE_CHECKING:
    from fakeyou.identity.identity import SpeakerRegistry, SpeakerRow
    from fakeyou.identity.voiceprint_embed import SpeakerEmbedder
    from fakeyou.identity.voiceprint_match import VoiceScores
    from fakeyou.observability.faults import FaultReporter


@dataclass(frozen=True, slots=True)
class CalibrationArtifact:
    theta_llr: float
    margin_floor: float
    snorm_cohort: tuple[float, ...]
    platt_a: float
    platt_b: float
    platt_version: str
    report_id: str
    model_version: str | None = None


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


def model_digest(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.rsplit(":", 1)[-1].strip().lower()
    if len(candidate) != 64:
        return None
    try:
        int(candidate, 16)
    except ValueError:
        return None
    return candidate


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
    model = _artifact_text(raw.get("model_version"))
    cohort_raw = raw.get("snorm_cohort")
    if (
        theta is None
        or floor is None
        or slope is None
        or bias is None
        or version is None
        or report is None
        or ("model_version" in raw and model is None)
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
        model_version=model,
    )


def claim_of(row: SpeakerRow) -> str | None:
    return row.display_claim


@dataclass(frozen=True, slots=True)
class VerificationInputs:
    artifact: CalibrationArtifact | None = None
    claim: str | None = None
    overlap: bool | None = None
    slot: int | None = None
    embedder: SpeakerEmbedder | None = None


DEFAULT_VERIFICATION_INPUTS = VerificationInputs()


def _model_context(
    uuid: str, verification: VerificationInputs, diagnostics: Diagnostics
) -> tuple[CalibrationArtifact, str, int] | None:
    artifact = verification.artifact
    embedder = verification.embedder
    slot = verification.slot
    if artifact is None or embedder is None or slot is None:
        return None
    model_id = embedder.model_id
    if not model_id:
        return None
    if artifact.model_version is not None:
        want = model_digest(artifact.model_version)
        got = model_digest(model_id)
        if want is None or got is None or want != got:
            if diagnostics.faults is not None:
                diagnostics.faults.record(
                    Fault(
                        source="audio",
                        kind="voiceprint_model_mismatch",
                        detail=(
                            "calibration model"
                            f" {str(artifact.model_version)[:16]} disagrees with embedder"
                            f" {str(model_id)[:16]} for speaker {uuid}"
                        ),
                    )
                )
            return None
    return artifact, model_id, slot


def _is_overlapped(registry: SpeakerRegistry, slot: int, *, overlap: bool | None) -> bool:
    if overlap is not None:
        return overlap
    return len(registry.stream_csrcs(slot)) > 1


def _scored_candidate(
    scores: VoiceScores, artifact: CalibrationArtifact
) -> tuple[float, float, float, float, str] | None:
    person = scores.person_id
    if person is None or scores.outcome == "negative":
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
    return mean, std, llr, margin, person


def _claim_owner(
    scores: VoiceScores, claim: str | None, uuid: str, faults: FaultReporter | None
) -> str | None:
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
    return owner


def _bind_person(
    registry: SpeakerRegistry,
    uuid: str,
    person: str,
    owner: str,
    faults: FaultReporter | None,
) -> str:
    bound = registry.note_verified(uuid, person, owner)
    if bound == "conflict":
        if faults is not None:
            faults.record(
                Fault(
                    source="audio",
                    kind="voiceprint_misbind",
                    detail=f"voice {person} conflicts with verified binding for speaker {uuid}",
                )
            )
    return bound


def decide_verified(
    uuid: str,
    scores: VoiceScores,
    verification: VerificationInputs,
    registry: SpeakerRegistry | None = None,
    diagnostics: Diagnostics = DEFAULT_DIAGNOSTICS,
) -> VerifiedDecision | None:
    journal = diagnostics.journal
    if registry is None or journal is None:
        return None
    model = _model_context(uuid, verification, diagnostics)
    if model is None:
        return None
    artifact, model_id, slot = model
    if _is_overlapped(registry, slot, overlap=verification.overlap):
        return None
    candidate = _scored_candidate(scores, artifact)
    if candidate is None:
        return None
    mean, std, llr, margin, person = candidate
    owner = _claim_owner(scores, verification.claim, uuid, diagnostics.faults)
    if owner is None:
        return None
    bound = _bind_person(registry, uuid, person, owner, diagnostics.faults)
    if bound in ("unknown", "conflict"):
        return None
    decision = VerifiedDecision(uuid=uuid, person_id=person, name=owner, llr=llr, margin=margin)
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
        snorm_n=len(artifact.snorm_cohort),
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
