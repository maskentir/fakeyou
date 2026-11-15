from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from fakeyou.foundation.abstention import AbstentionEntry, select_abstention
from fakeyou.foundation.claims import Claim, VerifiedClaim, parse_claims, verify_claim
from fakeyou.foundation.gathered_evidence import GatheredEvidence
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationContext,
    SentenceBinding,
    sentence_digest,
)
from fakeyou.foundation.rendering import RENDER_UNSUPPORTED, Rendered, render
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


def _distinct(claims: tuple[Claim, ...]) -> tuple[Claim, ...]:
    return tuple(dict.fromkeys(claims))


@dataclass(frozen=True, slots=True)
class Statement:
    rendered: Rendered
    verified: VerifiedClaim
    publication_id: str


@dataclass(slots=True)
class VisualTurn:
    speech: RuntimeSpeech
    evidence: GatheredEvidence | None
    needs: tuple[str, ...]
    statements: list[Statement] = field(default_factory=list)
    rejected: list[tuple[Claim, str]] = field(default_factory=list)

    async def speak(self, model_text: str) -> int:
        language = self.speech.identity.language
        for claim in _distinct(parse_claims(model_text)):
            verified = verify_claim(claim, self.evidence, language=language)
            rendered = render(verified, "current", language) if verified.supported else None
            if rendered is None:
                reason = RENDER_UNSUPPORTED if verified.supported else verified.support.reason
                self._reject(claim, reason)
                continue
            await self._state(rendered, verified)
        self._drop_model_text(model_text)
        if not self.statements:
            await self.speech.abstain(
                self.evidence,
                self.needs,
                reason=self.rejected[0][1] if self.rejected else "claim_undeclared",
            )
        return len(self.statements)

    def _reject(self, claim: Claim, reason: str) -> None:
        self.rejected.append((claim, reason))
        self.speech.journal.record(
            "visual_claim_rejected",
            source="agent",
            transcript_id=self.speech.identity.transcript_id,
            evidence_kind=claim.kind,
            shape=claim.shape,
            reason=reason,
        )

    def _drop_model_text(self, model_text: str) -> None:
        self.speech.journal.record(
            "model_sentence_dropped",
            source="agent",
            transcript_id=self.speech.identity.transcript_id,
            text_sha256=sentence_digest(model_text),
            chars=len(model_text),
            reason="model_extra_assertion_ignored" if self.statements else "claim_undeclared",
        )

    async def _state(self, rendered: Rendered, verified: VerifiedClaim) -> None:
        speech = self.speech
        look = verified.evidence
        if look is None or self.evidence is None:
            return
        index = speech.next_index()
        prepared = await speech.speaker.prepare(
            rendered.text,
            sentence_index=index,
            transcript_id=speech.identity.transcript_id,
            language=rendered.rendering.language,
        )
        if prepared is None:
            return
        binding = SentenceBinding(
            publication_id=speech.context.id_factory(),
            meeting_id=speech.context.meeting_id,
            turn_id=speech.identity.transcript_id,
            transcript_id=speech.identity.transcript_id,
            request_id=speech.identity.request_id,
            sentence_index=index,
            sentence_hash=sentence_digest(prepared.text),
            gather_attempt_id=self.evidence.gather_attempt_id,
            required_kinds=(look.kind,),
            evidence=(look,),
            scope=rendered.rendering.tense,
            dependence=rendered.claim.dependence,
            rendering=rendered.rendering,
        )
        speech.speaker.bind_publication(prepared, binding)
        self.statements.append(Statement(rendered, verified, binding.publication_id))
        speech.journal.record(
            "visual_statement_selected",
            source="agent",
            transcript_id=speech.identity.transcript_id,
            publication_id=binding.publication_id,
            pattern_id=rendered.rendering.pattern_id,
            pattern_version=rendered.rendering.version,
            language=rendered.rendering.language,
            evidence_kind=look.kind,
            fields=[(span.name, span.start, span.end) for span in rendered.rendering.fields],
        )
        await speech.speaker.speak_prepared(prepared)
        speech.spoken.append(prepared.text)
