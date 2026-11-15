from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fakeyou.foundation.abstention import AbstentionEntry, select_abstention
from fakeyou.foundation.gathered_evidence import GatheredEvidence
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationContext,
    SentenceBinding,
    sentence_digest,
)
from fakeyou.observability.journal import JournalSink
from fakeyou.speech.contract import PreparedSpeech

VISUAL_KINDS = frozenset({"screen", "participant", "camera"})


@runtime_checkable
class StatementSpeaker(Protocol):
    @property
    def publication_context(self) -> PublicationContext | None: ...

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None: ...

    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None: ...

    async def speak_prepared(self, prepared: PreparedSpeech) -> None: ...


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    transcript_id: str
    request_id: str
    language: str | None


def produced(evidence: GatheredEvidence | None, needs: tuple[str, ...]) -> bool:
    if evidence is None:
        return False
    required = set(needs)
    return any(item.kind in required and item.status == "produced" for item in evidence.provenance)


def _frame_inspected(item: EvidenceBinding) -> bool:
    return item.snapshot is not None and item.provider_attempt_id is not None


def abstention_for(
    evidence: GatheredEvidence | None, needs: tuple[str, ...], language: str | None
) -> tuple[AbstentionEntry, tuple[EvidenceBinding, ...]]:
    looks = () if evidence is None else tuple(i for i in evidence.provenance if i.kind in needs)
    inspected = tuple(i for i in looks if i.status == "insufficient" and _frame_inspected(i))
    if inspected:
        entry = select_abstention(
            look_status="insufficient", frame_inspected=True, language=language
        )
        return entry, inspected
    failed = any(item.status in {"insufficient", "unavailable"} for item in looks)
    status = "unavailable" if failed or not looks else None
    return select_abstention(look_status=status, frame_inspected=False, language=language), ()


@dataclass(slots=True)
class RuntimeSpeech:
    speaker: StatementSpeaker
    context: PublicationContext
    identity: TurnIdentity
    next_index: Callable[[], int]
    journal: JournalSink
    abstained: bool = False
    spoken: list[str] = field(default_factory=list)

    async def abstain(
        self, evidence: GatheredEvidence | None, needs: tuple[str, ...], *, reason: str
    ) -> bool:
        if self.abstained:
            self.journal.record(
                "abstention_skipped",
                source="agent",
                transcript_id=self.identity.transcript_id,
                reason=reason,
            )
            return False
        entry, looks = abstention_for(evidence, needs, self.identity.language)
        return await self.abstain_with(entry, evidence, looks, reason=reason)

    async def abstain_with(
        self,
        entry: AbstentionEntry,
        evidence: GatheredEvidence | None,
        looks: tuple[EvidenceBinding, ...],
        *,
        reason: str,
    ) -> bool:
        self.abstained = True
        index = self.next_index()
        prepared = await self.speaker.prepare(
            entry.text,
            sentence_index=index,
            transcript_id=self.identity.transcript_id,
            language=entry.language,
        )
        if prepared is None:
            return False
        historical = entry.scope == "historical"
        binding = SentenceBinding(
            publication_id=self.context.id_factory(),
            meeting_id=self.context.meeting_id,
            turn_id=self.identity.transcript_id,
            transcript_id=self.identity.transcript_id,
            request_id=self.identity.request_id,
            sentence_index=index,
            sentence_hash=sentence_digest(prepared.text),
            gather_attempt_id=None if evidence is None else evidence.gather_attempt_id,
            required_kinds=tuple(item.kind for item in looks) if historical else (),
            evidence=looks if historical else (),
            scope=entry.scope,
            abstention=entry.ref,
        )
        self.speaker.bind_publication(prepared, binding)
        self.journal.record(
            "abstention_selected",
            source="agent",
            transcript_id=self.identity.transcript_id,
            publication_id=binding.publication_id,
            entry_id=entry.entry_id,
            catalog_version=entry.version,
            language=entry.language,
            reason=reason,
            gather_attempt_id=binding.gather_attempt_id,
            looks=[
                {
                    "kind": item.kind,
                    "status": item.status,
                    "snapshot_id": None if item.snapshot is None else item.snapshot.correlation_id,
                    "provider_attempt_id": item.provider_attempt_id,
                }
                for item in (() if evidence is None else evidence.provenance)
            ],
        )
        await self.speaker.speak_prepared(prepared)
        self.spoken.append(prepared.text)
        return True
