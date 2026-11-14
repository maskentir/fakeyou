from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, Unpack

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.publication import (
    AudioPublication,
    PublicationDecision,
    SentenceBinding,
)
from fakeyou.observability.publication_measurement import MeasurementOptions, measure_publication

ConsentState = Literal["granted", "denied", "unknown", "revoked"]
AUDIO_MEASUREMENT_SCOPE = "audio_measurement"
VERIFIED_IDENTITY_METHODS = frozenset({"watermark", "fingerprint"})
ParticipantGap = Literal[
    "participant_observer_unavailable",
    "participant_consent_unknown",
    "participant_consent_denied",
    "participant_consent_revoked",
    "participant_consent_scope_mismatch",
    "participant_consent_not_in_force",
    "participant_sentence_not_reported",
    "participant_observer_mismatch",
    "publication_identity_unverified",
    "participant_clock_unmapped",
]


@dataclass(frozen=True, slots=True)
class ParticipantConsent:
    observer_id: str
    state: ConsentState
    source: str
    scope: str = AUDIO_MEASUREMENT_SCOPE
    granted_at: float | None = None
    revoked_at: float | None = None

    def gap(self, started_at: float, ended_at: float) -> ParticipantGap | None:
        if self.state == "denied":
            return "participant_consent_denied"
        if self.state == "revoked" or (self.revoked_at is not None and self.revoked_at <= ended_at):
            return "participant_consent_revoked"
        if self.state != "granted" or not self.source:
            return "participant_consent_unknown"
        if self.scope != AUDIO_MEASUREMENT_SCOPE:
            return "participant_consent_scope_mismatch"
        if self.granted_at is None or self.granted_at > started_at:
            return "participant_consent_not_in_force"
        return None


@dataclass(frozen=True, slots=True)
class HeardSentence:
    observer_id: str
    publication_id: str
    transcript_id: str
    sentence_index: int
    sentence_hash: str
    identity_method: str
    started_ms: float
    ended_ms: float


class ParticipantAudioObserver(Protocol):
    @property
    def observer_id(self) -> str: ...

    def consent(self) -> ParticipantConsent: ...

    def heard(self, binding: SentenceBinding) -> HeardSentence | None: ...

    def instant(self, observer_ms: float) -> MappedInstant | None: ...


class LocalWriteReceipt(Protocol):
    @property
    def publication_id(self) -> str | None: ...

    @property
    def local_audio_written(self) -> bool: ...

    @property
    def settled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ParticipantObservation:
    publication: AudioPublication | None
    gap: ParticipantGap | None
    observer_id: str | None = None


def observe_participant(
    binding: SentenceBinding, observer: ParticipantAudioObserver | None
) -> ParticipantObservation:
    if observer is None:
        return ParticipantObservation(None, "participant_observer_unavailable")
    observer_id = observer.observer_id
    consent = observer.consent()
    if consent.observer_id != observer_id:
        return ParticipantObservation(None, "participant_observer_mismatch", observer_id)
    standing = _standing_gap(consent)
    if standing is not None:
        return ParticipantObservation(None, standing, observer_id)
    heard = observer.heard(binding)
    if heard is None:
        return ParticipantObservation(None, "participant_sentence_not_reported", observer_id)
    if heard.observer_id != observer_id:
        return ParticipantObservation(None, "participant_observer_mismatch", observer_id)
    if heard.identity_method not in VERIFIED_IDENTITY_METHODS:
        return ParticipantObservation(None, "publication_identity_unverified", observer_id)
    started = observer.instant(heard.started_ms)
    ended = observer.instant(heard.ended_ms)
    if started is None or ended is None or started.clock_domain != ended.clock_domain:
        return ParticipantObservation(None, "participant_clock_unmapped", observer_id)
    in_force = consent.gap(started.monotonic, ended.monotonic)
    if in_force is not None:
        return ParticipantObservation(None, in_force, observer_id)
    return ParticipantObservation(
        AudioPublication(
            publication_id=heard.publication_id,
            transcript_id=heard.transcript_id,
            sentence_index=heard.sentence_index,
            sentence_hash=heard.sentence_hash,
            started_at=started.monotonic,
            ended_at=ended.monotonic,
            clock_domain=started.clock_domain,
            observation_kind="participant_audible",
            uncertainty_seconds=max(started.uncertainty_seconds, ended.uncertainty_seconds),
            participant_consent=True,
        ),
        None,
        observer_id,
    )


def _standing_gap(consent: ParticipantConsent) -> ParticipantGap | None:
    if consent.state in ("denied", "revoked", "unknown") or not consent.source:
        return consent.gap(0.0, 0.0)
    if consent.scope != AUDIO_MEASUREMENT_SCOPE:
        return "participant_consent_scope_mismatch"
    return None


def local_write_observed(
    receipt: LocalWriteReceipt | None, binding: SentenceBinding
) -> bool | None:
    if receipt is None or receipt.publication_id != binding.publication_id:
        return None
    if receipt.local_audio_written:
        return True
    return False if receipt.settled else None


def measure_participant_publication(
    binding: SentenceBinding,
    observer: ParticipantAudioObserver | None,
    *,
    decision: PublicationDecision | None,
    clock_domain: str,
    receipt: LocalWriteReceipt | None = None,
    **options: Unpack[MeasurementOptions],
) -> dict[str, Any]:
    observation = observe_participant(binding, observer)
    options.setdefault("local_write_observed", local_write_observed(receipt, binding))
    result = measure_publication(
        binding,
        observation.publication,
        decision=decision,
        clock_domain=clock_domain,
        **options,
    )
    result["participant_observer_id"] = observation.observer_id
    result["participant_gap"] = observation.gap
    if observation.gap is not None and result.get("reason") in _GENERIC_GAPS:
        result["reason"] = observation.gap
    return result


_GENERIC_GAPS = frozenset({"no_participant_observation", "publication_outcome_unknown"})
