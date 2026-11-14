from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fakeyou.perception.evidence import SnapshotRef

EVIDENCE_MAX_AGE_SECONDS = 5.0
PRE_PUBLICATION_MAX_AGE_SECONDS = 0.25
MAX_UNCERTAINTY_SECONDS = 0.05
PARTICIPANT_AUDIBLE = "participant_audible"
LOCAL_WRITE = "local_write"
CURRENT_SCOPE = "current"
NO_VISUAL_SCOPE = "no_visual"


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
        kinds = [item.kind for item in self.evidence]
        if len(set(kinds)) != len(kinds):
            raise ValueError("one required visual kind cannot carry two observations")
        if not set(self.required_kinds) <= set(kinds):
            raise ValueError("required visual kinds are not covered by the evidence")
        if self.scope == NO_VISUAL_SCOPE:
            if self.evidence or self.required_kinds:
                raise ValueError("a no-visual sentence carries no visual evidence")
            return
        if not self.evidence:
            raise ValueError("a scoped sentence needs the evidence it was gathered from")
        if any(item.gather_attempt_id != self.gather_attempt_id for item in self.evidence):
            raise ValueError("every observation belongs to the gather attempt of the sentence")


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
        emit: Callable[[], Awaitable[AudioPublication | None]],
    ) -> dict[str, Any]:
        binding = self._bindings.get(publication_id)
        if binding is None:
            return self._report(publication_id, None, None, "no_registered_sentence")
        before = await self._revalidate(binding, revalidate)
        gap = self._domain_gap(before)
        if gap is not None:
            return self._report(publication_id, binding, None, gap)
        publication = await emit()
        if publication is None:
            return self._report(publication_id, binding, None, "no_publication_observed")
        gap = self._publication_gap(binding, publication)
        if gap is not None:
            return self._report(publication_id, binding, publication, gap)
        gap = self._ordering_gap(before, publication)
        if gap is not None:
            return self._report(publication_id, binding, publication, gap)
        after = await self._revalidate(binding, revalidate)
        gap = self._domain_gap(after)
        if gap is not None:
            return self._report(publication_id, binding, publication, gap)
        gap = self._audible_gap(after, publication)
        if gap is not None:
            return self._report(publication_id, binding, publication, gap)
        gap = self._continuity_gap(binding, after)
        if gap is not None:
            return self._report(publication_id, binding, publication, gap)
        violations = self._violations(binding, before, after, publication)
        return self._report(publication_id, binding, publication, None, violations=violations)

    async def _revalidate(
        self,
        binding: SentenceBinding,
        revalidate: Callable[[EvidenceBinding], Awaitable[Validation]],
    ) -> list[tuple[EvidenceBinding, Validation]]:
        found: list[tuple[EvidenceBinding, Validation]] = []
        for evidence in binding.evidence:
            found.append((evidence, await revalidate(evidence)))
        return found

    def _domain_gap(self, found: list[tuple[EvidenceBinding, Validation]]) -> str | None:
        if any(validation.clock_domain != self._clock_domain for _evidence, validation in found):
            return "clock_domain_mismatch"
        return None

    def _publication_gap(
        self, binding: SentenceBinding, publication: AudioPublication
    ) -> str | None:
        if publication.clock_domain != self._clock_domain:
            return "clock_domain_mismatch"
        if publication.observation_kind != PARTICIPANT_AUDIBLE:
            return "not_participant_audible"
        if publication.publication_id != binding.publication_id:
            return "publication_identity_mismatch"
        if (
            publication.transcript_id != binding.transcript_id
            or publication.sentence_index != binding.sentence_index
            or publication.sentence_hash != binding.sentence_hash
        ):
            return "publication_identity_mismatch"
        if publication.ended_at < publication.started_at:
            return "publication_interval_invalid"
        return None

    def _ordering_gap(
        self,
        before: list[tuple[EvidenceBinding, Validation]],
        publication: AudioPublication,
    ) -> str | None:
        if any(validation.checked_at > publication.started_at for _e, validation in before):
            return "validation_after_publication"
        return None

    def _audible_gap(
        self,
        after: list[tuple[EvidenceBinding, Validation]],
        publication: AudioPublication,
    ) -> str | None:
        if any(validation.checked_at < publication.started_at for _e, validation in after):
            return "publication_validated_before_publication"
        return None

    def _continuity_gap(
        self, binding: SentenceBinding, after: list[tuple[EvidenceBinding, Validation]]
    ) -> str | None:
        if binding.scope != CURRENT_SCOPE:
            return None
        for _evidence, validation in after:
            if validation.unchanged is None:
                return "continuity_unknown"
            if validation.current_snapshot is None:
                return "current_snapshot_missing"
        return None

    def _violations(
        self,
        binding: SentenceBinding,
        before: list[tuple[EvidenceBinding, Validation]],
        after: list[tuple[EvidenceBinding, Validation]],
        publication: AudioPublication,
    ) -> list[str]:
        violations: list[str] = []
        for evidence, validation in before:
            violations.extend(
                self._freshness_violations(binding, evidence, validation, publication)
            )
        for evidence, validation in after:
            violations.extend(self._continuity_violations(binding, evidence, validation))
        return violations

    def _freshness_violations(
        self,
        binding: SentenceBinding,
        evidence: EvidenceBinding,
        validation: Validation,
        publication: AudioPublication,
    ) -> list[str]:
        violations: list[str] = []
        if (
            binding.scope == CURRENT_SCOPE
            and publication.started_at - evidence.observed_at > EVIDENCE_MAX_AGE_SECONDS
        ):
            violations.append("evidence_stale_at_publication")
        if publication.started_at - validation.checked_at > PRE_PUBLICATION_MAX_AGE_SECONDS:
            violations.append("prepublication_validation_too_old")
        if validation.uncertainty_seconds > MAX_UNCERTAINTY_SECONDS:
            violations.append("validation_uncertainty_too_large")
        if publication.uncertainty_seconds > MAX_UNCERTAINTY_SECONDS:
            violations.append("publication_uncertainty_too_large")
        if not validation.supported:
            violations.append("validation_not_supported")
        if validation.observed_at != evidence.observed_at:
            violations.append("validation_observation_mismatch")
        return violations

    def _continuity_violations(
        self,
        binding: SentenceBinding,
        evidence: EvidenceBinding,
        validation: Validation,
    ) -> list[str]:
        if binding.scope != CURRENT_SCOPE:
            return []
        violations: list[str] = []
        if validation.unchanged is False:
            violations.append("scene_not_continuous")
        if validation.current_snapshot is not None and _scene_identity(
            validation.current_snapshot
        ) != _scene_identity(evidence.snapshot):
            violations.append("scene_not_continuous")
        return violations

    def _report(
        self,
        publication_id: str,
        binding: SentenceBinding | None,
        publication: AudioPublication | None,
        reason: str | None,
        violations: list[str] | None = None,
    ) -> dict[str, Any]:
        measurable = reason is None
        report: dict[str, Any] = {
            "measurable": measurable,
            "compliant": (not violations) if measurable else None,
            "publication_id": publication_id,
        }
        if reason is not None:
            report["reason"] = reason
        if violations:
            report["violations"] = sorted(set(violations))
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
            report["measured_at"] = self._now()
        return report
