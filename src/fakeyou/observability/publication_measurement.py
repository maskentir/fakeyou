from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, TypedDict, Unpack

from fakeyou.foundation.publication import (
    AudioPublication,
    EvidenceBinding,
    PublicationDecision,
    PublicationValidity,
    SentenceBinding,
    SnapshotRef,
    Validation,
)


class MeasurementOptions(TypedDict, total=False):
    local_write_observed: bool | None
    post_validations: Mapping[str, Validation] | None


def measure_publication(
    binding: SentenceBinding,
    publication: AudioPublication | None,
    *,
    decision: PublicationDecision | None = None,
    clock_domain: str,
    **options: Unpack[MeasurementOptions],
) -> dict[str, Any]:
    result = _base_result(binding)
    if decision is not None and not decision.allows:
        if options.get("local_write_observed") is False and publication is None:
            result["status"] = (
                "prevented_invalid" if decision.validity == "invalid" else "prevented_unknown"
            )
            result["reason"] = decision.reason
            return result
        return _not_measurable(result, "publication_outcome_unknown")
    if publication is None:
        return _not_measurable(result, "no_participant_observation")
    reason = _observation_reason(binding, publication, clock_domain)
    if reason is not None:
        return _not_measurable(result, reason)
    checks = _current_checks(binding, publication, decision, options.get("post_validations"))
    invalid = next((reason for state, reason in checks if state == "invalid"), None)
    unknown = next((reason for state, reason in checks if state == "unknown"), None)
    if invalid is None and unknown is not None:
        return _not_measurable(result, unknown)
    result["status"] = "observed_compliant" if invalid is None else "observed_noncompliant"
    result["compliant"] = invalid is None
    if invalid is not None:
        result["reason"] = invalid
    return result


def _base_result(binding: SentenceBinding) -> dict[str, Any]:
    return {
        "publication_id": binding.publication_id,
        "meeting_id": binding.meeting_id,
        "turn_id": binding.turn_id,
        "request_id": binding.request_id,
        "transcript_id": binding.transcript_id,
        "sentence_index": binding.sentence_index,
        "sentence_hash": binding.sentence_hash,
        "gather_attempt_id": binding.gather_attempt_id,
        "scope": binding.scope,
    }


def _observation_reason(
    binding: SentenceBinding, publication: AudioPublication, clock_domain: str
) -> str | None:
    if not (
        publication.publication_id == binding.publication_id
        and publication.transcript_id == binding.transcript_id
        and publication.sentence_index == binding.sentence_index
        and publication.sentence_hash == binding.sentence_hash
    ):
        return "publication_identity_mismatch"
    if publication.observation_kind != "participant_audible":
        return "local_write_is_not_observation"
    if publication.participant_consent is not True:
        return "participant_consent_unknown"
    if any(not item.provider or not item.provider_attempt_id for item in binding.evidence):
        return "provider_identity_unmeasurable"
    if publication.clock_domain != clock_domain or not _valid_uncertainty(
        publication.uncertainty_seconds
    ):
        return "participant_clock_unmeasurable"
    if (
        not _finite_nonnegative(publication.started_at)
        or not _finite_nonnegative(publication.ended_at)
        or publication.ended_at < publication.started_at
    ):
        return "participant_interval_invalid"
    return None


def _current_checks(
    binding: SentenceBinding,
    publication: AudioPublication,
    decision: PublicationDecision | None,
    post_validations: Mapping[str, Validation] | None,
) -> list[tuple[PublicationValidity, str]]:
    if binding.scope != "current":
        return []
    checks = [_prevalidation_check(publication, decision)]
    for evidence in binding.evidence:
        checks.append(_capture_check(evidence, publication))
        checks.append(
            _continuity_check(
                evidence,
                publication,
                None if post_validations is None else post_validations.get(evidence.kind),
            )
        )
    return checks


def _prevalidation_check(
    publication: AudioPublication, decision: PublicationDecision | None
) -> tuple[PublicationValidity, str]:
    if (
        decision is None
        or not decision.allows
        or decision.checked_at is None
        or decision.clock_domain != publication.clock_domain
        or not _finite_nonnegative(decision.checked_at)
        or not _valid_uncertainty(decision.uncertainty_seconds)
    ):
        return "unknown", "current_gate_unproven"
    return _age_check(
        publication.started_at - decision.checked_at,
        publication.uncertainty_seconds + decision.uncertainty_seconds,
        0.25,
        "prepublication_validation",
    )


def _capture_check(
    evidence: EvidenceBinding, publication: AudioPublication
) -> tuple[PublicationValidity, str]:
    uncertainty = evidence.capture_mapping_uncertainty_seconds
    if (
        evidence.snapshot is None
        or evidence.capture_monotonic is None
        or evidence.capture_mapping_clock_domain != publication.clock_domain
        or not evidence.capture_time_origin
        or not evidence.capture_clock_domain
        or not _finite_nonnegative(evidence.capture_monotonic)
        or uncertainty is None
        or not _valid_uncertainty(uncertainty)
    ):
        return "unknown", "capture_age_unmeasurable"
    return _age_check(
        publication.started_at - evidence.capture_monotonic,
        publication.uncertainty_seconds + uncertainty,
        5.0,
        "capture_age",
    )


def _age_check(
    age: float, uncertainty: float, limit: float, name: str
) -> tuple[PublicationValidity, str]:
    if age - uncertainty < 0:
        return "unknown", f"{name}_ordering_unmeasurable"
    if age - uncertainty > limit:
        return "invalid", f"{name}_too_old"
    if age + uncertainty > limit:
        return "unknown", f"{name}_threshold_uncertain"
    return "valid", "ok"


def _continuity_check(
    evidence: EvidenceBinding, publication: AudioPublication, validation: Validation | None
) -> tuple[PublicationValidity, str]:
    if (
        validation is None
        or validation.clock_domain != publication.clock_domain
        or not _finite_nonnegative(validation.checked_at)
        or not _valid_uncertainty(validation.uncertainty_seconds)
        or validation.checked_at - validation.uncertainty_seconds
        < publication.ended_at + publication.uncertainty_seconds
    ):
        return "unknown", "postpublication_continuity_unmeasurable"
    if validation.supported is False or validation.unchanged is False:
        return "invalid", "scene_not_continuous"
    if (
        validation.supported is not True
        or validation.unchanged is not True
        or validation.current_snapshot is None
        or evidence.snapshot is None
    ):
        return "unknown", "postpublication_continuity_unmeasurable"
    if _scene_identity(validation.current_snapshot) != _scene_identity(evidence.snapshot):
        return "invalid", "scene_not_continuous"
    return "valid", "ok"


def _scene_identity(snapshot: SnapshotRef) -> tuple[object, ...]:
    return (
        snapshot.source,
        snapshot.subject_id,
        snapshot.stream_id,
        snapshot.epoch,
        snapshot.generation,
    )


def _not_measurable(result: dict[str, Any], reason: str) -> dict[str, Any]:
    result["status"] = "not_measurable"
    result["reason"] = reason
    return result


def _finite_nonnegative(value: float) -> bool:
    return math.isfinite(value) and value >= 0


def _valid_uncertainty(value: float) -> bool:
    return _finite_nonnegative(value) and value <= 0.05
