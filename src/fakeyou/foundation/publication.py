from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock import monotonic

EvidenceSource = Literal["camera", "screen"]
PublicationValidity = Literal["valid", "invalid", "unknown"]


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
class PublicationDecision:
    validity: PublicationValidity
    reason: str
    checked_at: float | None
    clock_domain: str | None
    uncertainty_seconds: float = 0.0

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
    decisions = [
        await _evaluate_evidence(item, revalidate, now, clock_domain) for item in binding.evidence
    ]
    boundary = now()
    for evidence in binding.evidence:
        capture = _capture_decision(evidence, boundary, clock_domain)
        if capture is not None:
            decisions.append(capture)
    decisions.extend(
        _validation_age_decision(item, boundary, clock_domain)
        for item in tuple(decisions)
        if item.allows
    )
    invalid = next((item for item in decisions if item.validity == "invalid"), None)
    if invalid is not None:
        return invalid
    unknown = next((item for item in decisions if item.validity == "unknown"), None)
    return unknown or PublicationDecision(
        "valid",
        "ok",
        min(item.checked_at for item in decisions if item.checked_at is not None),
        clock_domain,
        max(item.uncertainty_seconds for item in decisions),
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


async def _evaluate_evidence(
    evidence: EvidenceBinding,
    revalidate: Callable[[EvidenceBinding], Awaitable[Validation]],
    now: Callable[[], float],
    clock_domain: str,
) -> PublicationDecision:
    validation = await revalidate(evidence)
    checked_at = now()
    if not _finite(checked_at):
        return PublicationDecision("unknown", "invalid_check_time", None, clock_domain)
    capture_decision = _capture_decision(evidence, checked_at, clock_domain)
    if capture_decision is not None:
        return capture_decision
    if evidence.snapshot is None:
        return _unknown("snapshot_missing", checked_at, clock_domain)
    validation_decision = _validation_decision(validation, evidence, checked_at, clock_domain)
    if validation_decision is not None:
        return validation_decision
    if validation.current_snapshot is None or validation.unchanged is None:
        return _unknown("scene_continuity_unknown", checked_at, clock_domain)
    if validation.unchanged is False:
        return PublicationDecision("invalid", "scene_changed", checked_at, clock_domain)
    if _scene_identity(validation.current_snapshot) != _scene_identity(evidence.snapshot):
        return PublicationDecision("invalid", "scene_changed", checked_at, clock_domain)
    return PublicationDecision(
        "valid", "ok", validation.checked_at, clock_domain, validation.uncertainty_seconds
    )


def _capture_decision(
    binding: EvidenceBinding, checked_at: float, clock_domain: str
) -> PublicationDecision | None:
    if not _finite(checked_at):
        return _unknown("invalid_check_time", checked_at, clock_domain)
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
    if uncertainty < 0 or uncertainty > 0.05:
        return _unknown("capture_time_invalid", checked_at, clock_domain)
    if binding.capture_monotonic < 0:
        return _unknown("capture_time_invalid", checked_at, clock_domain)
    if binding.capture_monotonic > checked_at:
        return _unknown("capture_time_in_future", checked_at, clock_domain)
    age = checked_at - binding.capture_monotonic
    if age - uncertainty > 5.0:
        return PublicationDecision("invalid", "capture_too_old", checked_at, clock_domain)
    if age + uncertainty > 5.0:
        return _unknown("capture_age_threshold_uncertain", checked_at, clock_domain)
    return None


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
        or validation.uncertainty_seconds > 0.05
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
    if age - decision.uncertainty_seconds > 0.25:
        return PublicationDecision(
            "invalid",
            "validation_too_old",
            decision.checked_at,
            clock_domain,
            decision.uncertainty_seconds,
        )
    if age + decision.uncertainty_seconds > 0.25:
        return _unknown("validation_age_threshold_uncertain", boundary, clock_domain)
    return decision
