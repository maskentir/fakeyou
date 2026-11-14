from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fakeyou.perception.evidence import SnapshotRef

EVIDENCE_MAX_AGE_SECONDS = 5.0
PRE_PUBLICATION_MAX_AGE_SECONDS = 0.25
PARTICIPANT_AUDIBLE = "participant_audible"
LOCAL_WRITE = "local_write"


@dataclass(frozen=True, slots=True)
class EvidenceBinding:
    kind: str
    gather_attempt_id: str
    snapshot: SnapshotRef
    observed_at: float
    provider: str
    provider_attempt_id: str
    inference: str


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

    def __post_init__(self) -> None:
        if len(set(self.required_kinds)) != len(self.required_kinds):
            raise ValueError("required visual kinds must be unique")
        covered = {item.kind for item in self.evidence}
        if not set(self.required_kinds) <= covered:
            raise ValueError("required visual kinds are not covered by the evidence")
        if self.scope != "no_visual" and not self.evidence:
            raise ValueError("a scoped sentence needs the evidence it was gathered from")


@dataclass(frozen=True, slots=True)
class Validation:
    checked_at: float
    clock_domain: str
    current_snapshot: SnapshotRef | None
    supported: bool
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


def _scene_identity(snapshot: SnapshotRef) -> tuple[Any, ...]:
    return (
        snapshot.source,
        snapshot.subject_id,
        snapshot.stream_id,
        snapshot.epoch,
        snapshot.generation,
    )


def _snapshot_json(snapshot: SnapshotRef) -> dict[str, Any]:
    return {
        "source": snapshot.source,
        "subject_id": snapshot.subject_id,
        "stream_id": snapshot.stream_id,
        "epoch": snapshot.epoch,
        "sequence": snapshot.sequence,
        "generation": snapshot.generation,
        "captured_at": snapshot.captured_at.isoformat(),
    }


class PublicationProbe:
    def __init__(self, now: Callable[[], float], clock_domain: str) -> None:
        self._now = now
        self._clock_domain = clock_domain
        self._bindings: dict[str, SentenceBinding] = {}

    def register(self, binding: SentenceBinding) -> None:
        if binding.publication_id in self._bindings:
            raise ValueError("publication identity must be unique")
        self._bindings[binding.publication_id] = binding

    async def publish(
        self,
        publication_id: str,
        *,
        revalidate: Callable[[EvidenceBinding], Awaitable[Validation]],
        emit: Callable[[], Awaitable[AudioPublication]],
    ) -> dict[str, Any]:
        binding = self._bindings.get(publication_id)
        if binding is None:
            return self._report(publication_id, None, None, None, "no_registered_sentence")
        now = self._now()
        for evidence in binding.evidence:
            validation = await revalidate(evidence)
            failure = self._pre_publication_failure(evidence, validation, now)
            if failure is not None:
                return self._report(publication_id, binding, None, None, failure)
        publication = await emit()
        failure = self._publication_failure(binding, publication)
        if failure is not None:
            return self._report(publication_id, binding, publication, None, failure)
        for evidence in binding.evidence:
            validation = await revalidate(evidence)
            failure = self._continuity_failure(binding, evidence, validation)
            if failure is not None:
                return self._report(publication_id, binding, publication, None, failure)
        return self._report(publication_id, binding, publication, now, None)

    def _pre_publication_failure(
        self, evidence: EvidenceBinding, validation: Validation, now: float
    ) -> str | None:
        if validation.clock_domain != self._clock_domain:
            return "clock_domain_mismatch"
        if now - validation.checked_at > PRE_PUBLICATION_MAX_AGE_SECONDS:
            return "prepublication_validation_too_old"
        if now - evidence.observed_at > EVIDENCE_MAX_AGE_SECONDS:
            return "evidence_too_old"
        if now - validation.observed_at > EVIDENCE_MAX_AGE_SECONDS:
            return "evidence_too_old"
        if not validation.supported:
            return "validation_not_supported"
        return None

    def _publication_failure(
        self, binding: SentenceBinding, publication: AudioPublication
    ) -> str | None:
        if publication.publication_id != binding.publication_id:
            return "publication_identity_mismatch"
        if publication.observation_kind != PARTICIPANT_AUDIBLE:
            return "not_participant_audible"
        if publication.clock_domain != self._clock_domain:
            return "clock_domain_mismatch"
        if binding.evidence and (
            publication.transcript_id != binding.transcript_id
            or publication.sentence_index != binding.sentence_index
            or publication.sentence_hash != binding.sentence_hash
        ):
            return "publication_identity_mismatch"
        return None

    def _continuity_failure(
        self, binding: SentenceBinding, evidence: EvidenceBinding, validation: Validation
    ) -> str | None:
        if binding.scope != "current":
            return None
        if validation.unchanged is False:
            return "scene_not_continuous"
        if validation.current_snapshot is not None and _scene_identity(
            validation.current_snapshot
        ) != _scene_identity(evidence.snapshot):
            return "scene_not_continuous"
        return None

    def _report(
        self,
        publication_id: str,
        binding: SentenceBinding | None,
        publication: AudioPublication | None,
        now: float | None,
        reason: str | None,
    ) -> dict[str, Any]:
        report: dict[str, Any] = {"measurable": reason is None, "publication_id": publication_id}
        if reason is not None:
            report["reason"] = reason
        if binding is not None:
            report["meeting_id"] = binding.meeting_id
            report["turn_id"] = binding.turn_id
            report["transcript_id"] = binding.transcript_id
            report["request_id"] = binding.request_id
            report["sentence_index"] = binding.sentence_index
            report["sentence_hash"] = binding.sentence_hash
            report["gather_attempt_id"] = binding.gather_attempt_id
            report["required_kinds"] = list(binding.required_kinds)
            report["scope"] = binding.scope
            report["clock_domain"] = self._clock_domain
            report["evidence"] = [
                {
                    "kind": item.kind,
                    "gather_attempt_id": item.gather_attempt_id,
                    "provider": item.provider,
                    "provider_attempt_id": item.provider_attempt_id,
                    "inference": item.inference,
                    "observed_at": item.observed_at,
                    "snapshot": _snapshot_json(item.snapshot),
                }
                for item in binding.evidence
            ]
        if publication is not None:
            report["publication"] = {
                "publication_id": publication.publication_id,
                "transcript_id": publication.transcript_id,
                "sentence_index": publication.sentence_index,
                "sentence_hash": publication.sentence_hash,
                "started_at": publication.started_at,
                "ended_at": publication.ended_at,
                "clock_domain": publication.clock_domain,
                "observation_kind": publication.observation_kind,
                "uncertainty_seconds": publication.uncertainty_seconds,
            }
        if now is not None:
            report["measured_at"] = now
        return report
