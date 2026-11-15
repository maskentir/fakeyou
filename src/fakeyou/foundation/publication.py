from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.content_digest import ContentSample, MatchLevel

EvidenceSource = Literal["camera", "screen"]
PublicationValidity = Literal["valid", "invalid", "unknown"]
AgeState = Literal["fresh", "uncertain", "old"]
CAPTURE_FRESHNESS_SECONDS = 5.0
VALIDATION_AGE_SECONDS = 0.25
MAX_CLOCK_UNCERTAINTY_SECONDS = 0.05
VALID_REASONS = ("ok", "continuity_confirmed", "content_confirmed_after_interruption")


@dataclass(frozen=True, slots=True)
class SnapshotRef:
    source: EvidenceSource
    subject_id: str
    stream_id: str
    epoch: int
    sequence: int
    generation: int
    captured_at: datetime

    @property
    def correlation_id(self) -> str:
        digest = hashlib.sha256(
            "|".join(
                (
                    self.source,
                    self.subject_id,
                    self.stream_id,
                    str(self.epoch),
                    str(self.sequence),
                    str(self.generation),
                )
            ).encode()
        ).hexdigest()
        return digest[:16]


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    kind: str
    gather_attempt_id: str
    snapshot: SnapshotRef | None
    observed_at: float
    provider: str | None
    provider_attempt_id: str | None
    inference: str
    capture_monotonic: float | None = None
    capture_clock_domain: str | None = None
    capture_mapping_clock_domain: str | None = None
    capture_time_origin: str | None = None
    capture_mapping_uncertainty_seconds: float | None = None
    observation_clock_domain: str | None = None
    status: str | None = None
    analyzed: ContentSample | None = None


@dataclass(frozen=True, slots=True)
class StateProvenance:
    question: str
    fact_id: str
    namespace: str
    modality: str
    domain: str
    revision: int
    valid_from_seq: int
    subject_id: str | None
    claim: str
    known: bool
    source_health: str | None


def sentence_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SentenceBinding:
    publication_id: str
    meeting_id: str
    turn_id: str
    transcript_id: str
    request_id: str
    sentence_index: int
    sentence_hash: str
    gather_attempt_id: str | None
    required_kinds: tuple[str, ...]
    evidence: tuple[EvidenceBinding, ...]
    scope: str
    state: StateProvenance | None = None

    def __post_init__(self) -> None:
        _validate_sentence_identity(self)
        _validate_sentence_scope(self)
        _validate_sentence_state(self)
        _validate_sentence_evidence(self)


def _validate_sentence_identity(binding: SentenceBinding) -> None:
    if any(
        not value
        for value in (
            binding.publication_id,
            binding.meeting_id,
            binding.turn_id,
            binding.transcript_id,
            binding.request_id,
            binding.sentence_hash,
        )
    ):
        raise ValueError("publication identity fields must be non-empty")
    if binding.gather_attempt_id is not None and not binding.gather_attempt_id:
        raise ValueError("gather attempt identity must be non-empty")


def _validate_sentence_scope(binding: SentenceBinding) -> None:
    if binding.scope not in {"current", "historical", "no_visual", "state"}:
        raise ValueError("unknown publication scope")
    if len(set(binding.required_kinds)) != len(binding.required_kinds):
        raise ValueError("required visual kinds must be unique")
    kinds = [item.kind for item in binding.evidence]
    if len(set(kinds)) != len(kinds):
        raise ValueError("one required visual kind cannot carry two observations")
    if not set(binding.required_kinds) <= set(kinds):
        raise ValueError("required visual kinds are not covered by the evidence")
    if binding.scope == "no_visual" and (binding.evidence or binding.required_kinds):
        raise ValueError("a no-visual sentence carries no visual evidence")
    if binding.scope == "state" and (binding.evidence or binding.required_kinds):
        raise ValueError("a state sentence carries state provenance, not visual evidence")


def _validate_sentence_state(binding: SentenceBinding) -> None:
    state = binding.state
    if binding.scope == "state":
        if state is None or not state.known:
            raise ValueError("a state sentence needs the known fact it was rendered from")
        if not state.fact_id or not state.question or not state.claim:
            raise ValueError("state provenance identity fields must be non-empty")
        return
    if state is None:
        return
    if state.known or binding.scope != "no_visual":
        raise ValueError("only an abstention carries state provenance outside the state scope")


def _validate_sentence_evidence(binding: SentenceBinding) -> None:
    if binding.scope in {"no_visual", "state"}:
        return
    if not binding.evidence:
        raise ValueError("a scoped sentence needs the evidence it was gathered from")
    if any(item.gather_attempt_id != binding.gather_attempt_id for item in binding.evidence):
        raise ValueError("every observation belongs to the gather attempt of the sentence")
    if any(kind not in {"camera", "participant", "screen"} for kind in binding.required_kinds):
        raise ValueError("unknown visual kind")
    if any(item.kind not in {"camera", "participant", "screen"} for item in binding.evidence):
        raise ValueError("unknown visual kind")
    if any(
        item.snapshot is not None
        and item.snapshot.source != ("camera" if item.kind == "participant" else item.kind)
        for item in binding.evidence
    ):
        raise ValueError("visual kind and snapshot source differ")


@dataclass(frozen=True, slots=True)
class Validation:
    checked_at: float
    clock_domain: str
    current_snapshot: SnapshotRef | None
    supported: bool | None
    unchanged: bool | None
    observed_at: float
    uncertainty_seconds: float
    match: MatchLevel | None = None
    confirmation: ContentSample | None = None
    source_stopped: bool = False


@dataclass(frozen=True, slots=True)
class AudioPublication:
    publication_id: str
    transcript_id: str
    sentence_index: int
    sentence_hash: str
    started_at: float
    ended_at: float
    clock_domain: str
    observation_kind: str
    uncertainty_seconds: float
    participant_consent: bool | None = None


@dataclass(frozen=True, slots=True)
class EvidenceBasis:
    kind: str
    analyzed: ContentSample | None
    confirmation: ContentSample | None
    match: MatchLevel | None
    source_continuous: bool | None
    evaluated_at: float


@dataclass(frozen=True, slots=True)
class PublicationDecision:
    validity: PublicationValidity
    reason: str
    checked_at: float | None
    clock_domain: str | None
    uncertainty_seconds: float = 0.0
    basis: tuple[EvidenceBasis, ...] = ()

    @property
    def allows(self) -> bool:
        return self.validity == "valid"


@dataclass(frozen=True, slots=True)
class PublicationContext:
    meeting_id: str
    clock_domain: str
    gate: Callable[[SentenceBinding], Awaitable[PublicationDecision | None]] | None = None
    now: Callable[[], float] = monotonic
    timeout_seconds: float = 0.25
    id_factory: Callable[[], str] = lambda: str(uuid.uuid4())
    commit: Callable[[SentenceBinding], PublicationDecision | None] | None = None
    revision: Callable[[], int] | None = None
    samples_since: Callable[[int], tuple[ContentSample, ...]] | None = None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _scene_identity(snapshot: SnapshotRef) -> tuple[object, ...]:
    return (
        snapshot.source,
        snapshot.subject_id,
        snapshot.stream_id,
        snapshot.epoch,
        snapshot.generation,
    )


def _unknown(reason: str, now: float, clock_domain: str) -> PublicationDecision:
    return PublicationDecision("unknown", reason, now, clock_domain)


async def evaluate_visual_publication(
    binding: SentenceBinding,
    revalidate: Callable[[EvidenceBinding], Awaitable[Validation]],
    *,
    now: Callable[[], float],
    clock_domain: str,
) -> PublicationDecision:
    if binding.scope in {"no_visual", "historical"}:
        return PublicationDecision("valid", "ok", None, clock_domain)
    if binding.scope == "state":
        return _unknown("state_scope_needs_state_validation", now(), clock_domain)
    validations = {item.kind: await revalidate(item) for item in binding.evidence}
    return decide_visual_publication(binding, validations, at=now(), clock_domain=clock_domain)


def decide_visual_publication(
    binding: SentenceBinding,
    validations: Mapping[str, Validation],
    *,
    at: float,
    clock_domain: str,
) -> PublicationDecision:
    if binding.scope in {"no_visual", "historical"}:
        return PublicationDecision("valid", "ok", None, clock_domain)
    if binding.scope == "state":
        return _unknown("state_scope_needs_state_validation", at, clock_domain)
    if not _finite(at):
        return PublicationDecision("unknown", "invalid_check_time", None, clock_domain)
    decisions = [
        _evidence_decision(item, validations.get(item.kind), at, clock_domain)
        for item in binding.evidence
    ]
    basis = tuple(_basis(item, validations.get(item.kind), at) for item in binding.evidence)
    invalid = next((item for item in decisions if item.validity == "invalid"), None)
    unknown = next((item for item in decisions if item.validity == "unknown"), None)
    stopped = invalid or unknown
    if stopped is not None:
        return replace(stopped, basis=basis)
    return PublicationDecision(
        "valid",
        max((item.reason for item in decisions), key=VALID_REASONS.index),
        min(item.checked_at for item in decisions if item.checked_at is not None),
        clock_domain,
        max(item.uncertainty_seconds for item in decisions),
        basis,
    )


def evaluate_state_publication(
    binding: SentenceBinding,
    latest: Callable[[StateProvenance], StateProvenance | None],
    *,
    now: Callable[[], float],
    clock_domain: str,
) -> PublicationDecision:
    if binding.scope == "no_visual":
        return PublicationDecision("valid", "ok", None, clock_domain)
    claimed = binding.state
    if binding.scope != "state" or claimed is None:
        return _unknown("state_provenance_missing", now(), clock_domain)
    current = latest(claimed)
    checked_at = now()
    if not _finite(checked_at):
        return PublicationDecision("unknown", "invalid_check_time", None, clock_domain)
    if current is None or not current.known:
        return _unknown("state_source_unknown", checked_at, clock_domain)
    if current.revision < claimed.revision or current.valid_from_seq < claimed.valid_from_seq:
        return _unknown("state_revision_regressed", checked_at, clock_domain)
    if current.subject_id != claimed.subject_id:
        return PublicationDecision("invalid", "state_subject_changed", checked_at, clock_domain)
    if current.claim != claimed.claim:
        return PublicationDecision("invalid", "state_changed", checked_at, clock_domain)
    return PublicationDecision("valid", "ok", checked_at, clock_domain)


def _evidence_decision(
    evidence: EvidenceBinding, validation: Validation | None, at: float, clock_domain: str
) -> PublicationDecision:
    if validation is None:
        return _unknown("validation_missing", at, clock_domain)
    stopped = (
        _capture_mapping_decision(evidence, at, clock_domain)
        or _snapshot_decision(evidence, at, clock_domain)
        or _validation_decision(validation, evidence, at, clock_domain)
        or _continuity_decision(validation, evidence, at, clock_domain)
        or _content_decision(validation.match, at, clock_domain)
    )
    if stopped is not None:
        return stopped
    return _freshness_decision(evidence, validation, at, clock_domain)


def _snapshot_decision(
    evidence: EvidenceBinding, at: float, clock_domain: str
) -> PublicationDecision | None:
    if evidence.snapshot is None:
        return _unknown("snapshot_missing", at, clock_domain)
    return None


def _continuity_decision(
    validation: Validation, evidence: EvidenceBinding, at: float, clock_domain: str
) -> PublicationDecision | None:
    if validation.source_stopped:
        return PublicationDecision("invalid", "scene_changed", at, clock_domain)
    if validation.current_snapshot is None or validation.unchanged is None:
        return _unknown("scene_continuity_unknown", at, clock_domain)
    camera = evidence.snapshot is not None and evidence.snapshot.source == "camera"
    if validation.unchanged is False and (validation.match is None or camera):
        return PublicationDecision("invalid", "scene_changed", at, clock_domain)
    if (
        validation.match is None
        and evidence.snapshot is not None
        and _scene_identity(validation.current_snapshot) != _scene_identity(evidence.snapshot)
    ):
        return PublicationDecision("invalid", "scene_changed", at, clock_domain)
    return None


def _content_decision(
    match: MatchLevel | None, checked_at: float, clock_domain: str
) -> PublicationDecision | None:
    if match == "similar":
        return _unknown("content_changed_unverified", checked_at, clock_domain)
    if match == "distinct":
        return PublicationDecision("invalid", "scene_changed", checked_at, clock_domain)
    if match == "unknown":
        return _unknown("continuity_unknown", checked_at, clock_domain)
    return None


def _age_state(age: float, uncertainty: float) -> AgeState:
    if age - uncertainty > CAPTURE_FRESHNESS_SECONDS:
        return "old"
    if age + uncertainty > CAPTURE_FRESHNESS_SECONDS:
        return "uncertain"
    return "fresh"


def _capture_mapping_decision(
    binding: EvidenceBinding, checked_at: float, clock_domain: str
) -> PublicationDecision | None:
    if (
        not binding.capture_clock_domain
        or not binding.capture_time_origin
        or binding.capture_monotonic is None
    ):
        return _unknown("capture_time_unmapped", checked_at, clock_domain)
    if binding.capture_mapping_clock_domain != clock_domain:
        return _unknown("capture_clock_domain_mismatch", checked_at, clock_domain)
    if not _finite(binding.capture_monotonic) or not _finite(
        binding.capture_mapping_uncertainty_seconds
    ):
        return _unknown("capture_time_invalid", checked_at, clock_domain)
    uncertainty = binding.capture_mapping_uncertainty_seconds or 0.0
    if uncertainty < 0 or uncertainty > MAX_CLOCK_UNCERTAINTY_SECONDS:
        return _unknown("capture_time_invalid", checked_at, clock_domain)
    if binding.capture_monotonic < 0:
        return _unknown("capture_time_invalid", checked_at, clock_domain)
    if binding.capture_monotonic > checked_at:
        return _unknown("capture_time_in_future", checked_at, clock_domain)
    return None


def _confirmation_decision(
    evidence: EvidenceBinding, confirmation: ContentSample | None, at: float, clock_domain: str
) -> PublicationDecision | None:
    capture = None if confirmation is None else confirmation.capture
    captured = evidence.capture_monotonic
    if capture is None or captured is None or capture.clock_domain != clock_domain:
        return _unknown("confirmation_time_unmapped", at, clock_domain)
    uncertainty = capture.uncertainty_seconds
    if not (_finite(capture.monotonic) and _finite(uncertainty)):
        return _unknown("confirmation_time_unmapped", at, clock_domain)
    if uncertainty < 0 or uncertainty > MAX_CLOCK_UNCERTAINTY_SECONDS:
        return _unknown("confirmation_time_unmapped", at, clock_domain)
    if capture.monotonic < captured or capture.monotonic > at:
        return _unknown("confirmation_time_inconsistent", at, clock_domain)
    state = _age_state(at - capture.monotonic, uncertainty)
    if state == "old":
        return PublicationDecision("invalid", "confirmation_too_old", at, clock_domain)
    if state == "uncertain":
        return _unknown("confirmation_age_threshold_uncertain", at, clock_domain)
    return None


def _freshness_decision(
    evidence: EvidenceBinding, validation: Validation, at: float, clock_domain: str
) -> PublicationDecision:
    captured = evidence.capture_monotonic or 0.0
    state = _age_state(at - captured, evidence.capture_mapping_uncertainty_seconds or 0.0)
    reason = "ok"
    if validation.match == "exact":
        confirmation = validation.confirmation
        stopped = _confirmation_decision(evidence, confirmation, at, clock_domain)
        if stopped is not None and (state != "fresh" or stopped.validity == "unknown"):
            return stopped
        analyzed = evidence.analyzed
        interrupted = (
            confirmation is not None
            and analyzed is not None
            and confirmation.continuity != analyzed.continuity
        )
        if interrupted:
            reason = "content_confirmed_after_interruption"
        elif state != "fresh":
            reason = "continuity_confirmed"
    elif state == "old":
        return PublicationDecision("invalid", "capture_too_old", at, clock_domain)
    elif state == "uncertain":
        return _unknown("capture_age_threshold_uncertain", at, clock_domain)
    return PublicationDecision(
        "valid", reason, validation.checked_at, clock_domain, validation.uncertainty_seconds
    )


def _basis(evidence: EvidenceBinding, validation: Validation | None, at: float) -> EvidenceBasis:
    analyzed = evidence.analyzed
    confirmation = None if validation is None else validation.confirmation
    return EvidenceBasis(
        kind=evidence.kind,
        analyzed=analyzed,
        confirmation=confirmation,
        match=None if validation is None else validation.match,
        source_continuous=(
            None
            if analyzed is None or confirmation is None
            else analyzed.continuity == confirmation.continuity
        ),
        evaluated_at=at,
    )


def _validation_decision(
    validation: Validation,
    binding: EvidenceBinding,
    checked_at: float,
    clock_domain: str,
) -> PublicationDecision | None:
    if (
        validation.clock_domain != clock_domain
        or not _finite(validation.checked_at)
        or validation.checked_at < 0
    ):
        return _unknown("validation_clock_domain_mismatch", checked_at, clock_domain)
    if validation.checked_at > checked_at:
        return _unknown("validation_time_in_future", checked_at, clock_domain)
    if (
        not _finite(validation.uncertainty_seconds)
        or validation.uncertainty_seconds < 0
        or validation.uncertainty_seconds > MAX_CLOCK_UNCERTAINTY_SECONDS
    ):
        return _unknown("validation_uncertainty_too_large", checked_at, clock_domain)
    age = _validation_age_decision(
        PublicationDecision(
            "valid", "ok", validation.checked_at, clock_domain, validation.uncertainty_seconds
        ),
        checked_at,
        clock_domain,
    )
    if not age.allows:
        return age
    if validation.supported is None:
        return _unknown("validation_support_unknown", checked_at, clock_domain)
    if not validation.supported:
        return PublicationDecision("invalid", "unsupported", checked_at, clock_domain)
    if (
        binding.snapshot is not None
        and binding.snapshot.source == "camera"
        and validation.current_snapshot is not None
        and (binding.snapshot.subject_id != validation.current_snapshot.subject_id)
    ):
        return PublicationDecision("invalid", "subject_changed", checked_at, clock_domain)
    return None


def _validation_age_decision(
    decision: PublicationDecision, boundary: float, clock_domain: str
) -> PublicationDecision:
    if decision.checked_at is None or not _finite(boundary):
        return _unknown("invalid_check_time", boundary, clock_domain)
    age = boundary - decision.checked_at
    if age < 0:
        return _unknown("validation_time_in_future", boundary, clock_domain)
    if age - decision.uncertainty_seconds > VALIDATION_AGE_SECONDS:
        return PublicationDecision(
            "invalid",
            "validation_too_old",
            decision.checked_at,
            clock_domain,
            decision.uncertainty_seconds,
        )
    if age + decision.uncertainty_seconds > VALIDATION_AGE_SECONDS:
        return _unknown("validation_age_threshold_uncertain", boundary, clock_domain)
    return decision
