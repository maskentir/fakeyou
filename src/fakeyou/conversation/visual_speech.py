from __future__ import annotations

import asyncio
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
from fakeyou.speech.contract import OutcomeSource, PlaybackReceipt, PreparedSpeech

VISUAL_KINDS = frozenset({"screen", "participant", "camera"})
SYNTHESIS_UNAVAILABLE = "synthesis_unavailable"
NO_CURRENT_PATTERN = "no_current_pattern"
HISTORICAL_GATHER = "historical_gather"


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


HISTORICAL_REASONS = frozenset(
    {
        "capture_too_old",
        "confirmation_too_old",
        "confirmation_age_threshold_uncertain",
        "capture_age_threshold_uncertain",
        "content_changed_unverified",
        "continuity_unknown",
        "scene_changed",
        "scene_continuity_unknown",
        "source_identity_changed",
        "confirmation_time_unmapped",
        "confirmation_time_inconsistent",
        "write_delayed_past_validation",
        NO_CURRENT_PATTERN,
        HISTORICAL_GATHER,
    }
)
PREVENTED_PREFIX = "publication_prevented:"


def prevented_reason(receipt: PlaybackReceipt | None) -> str | None:
    if receipt is None or receipt.outcome != "prevented":
        return None
    return receipt.detail.removeprefix(PREVENTED_PREFIX).partition(":")[2] or "unresolved"


@dataclass(slots=True)
class RuntimeSpeech:
    speaker: StatementSpeaker
    context: PublicationContext
    identity: TurnIdentity
    next_index: Callable[[], int]
    journal: JournalSink
    abstained: bool = False
    spoken: list[str] = field(default_factory=list)

    async def prepare(
        self, text: str, language: str, *, purpose: str
    ) -> tuple[PreparedSpeech, int] | None:
        mode = self.context.runtime_synthesis
        common = {"transcript_id": self.identity.transcript_id, "purpose": purpose}
        if mode != "local":
            self.journal.record(
                "runtime_statement_skipped",
                source="agent",
                reason=SYNTHESIS_UNAVAILABLE
                if mode == "unavailable"
                else "provider_attempt_ledger_unavailable",
                statement=text,
                **common,
            )
            return None
        index = self.next_index()
        request = {"authorization": mode, "text_sha256": sentence_digest(text), **common}
        self.journal.record("runtime_preparation_requested", source="agent", **request)
        try:
            prepared = await self.speaker.prepare(
                text,
                sentence_index=index,
                transcript_id=self.identity.transcript_id,
                language=language,
            )
        except asyncio.CancelledError:
            self.journal.record("runtime_preparation_cancelled", source="agent", **request)
            raise
        except Exception as error:
            self.journal.record(
                "runtime_preparation_failed", source="agent", error=type(error).__name__, **request
            )
            raise
        self.journal.record(
            "runtime_preparation_finished",
            source="agent",
            outcome="empty" if prepared is None else "prepared",
            **request,
        )
        return None if prepared is None else (prepared, index)

    def outcome(self, publication_id: str) -> PlaybackReceipt | None:
        speaker = self.speaker
        if not isinstance(speaker, OutcomeSource):
            return None
        return speaker.publication_outcome(publication_id)

    async def publish(self, prepared: PreparedSpeech, binding: SentenceBinding) -> bool:
        self.speaker.bind_publication(prepared, binding)
        await self.speaker.speak_prepared(prepared)
        receipt = self.outcome(binding.publication_id)
        voiced = receipt is not None and receipt.local_audio_written
        if voiced:
            self.spoken.append(prepared.text)
        return voiced

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
        ready = await self.prepare(entry.text, entry.language, purpose="abstention")
        if ready is None:
            return False
        prepared, index = ready
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
        return await self.publish(prepared, binding)


def _distinct(claims: tuple[Claim, ...]) -> tuple[Claim, ...]:
    return tuple(dict.fromkeys(claims))


@dataclass(frozen=True, slots=True)
class Statement:
    rendered: Rendered
    verified: VerifiedClaim
    publication_id: str
    voiced: bool
    replaces: str | None = None


@dataclass(slots=True)
class VisualTurn:
    speech: RuntimeSpeech
    evidence: GatheredEvidence | None
    needs: tuple[str, ...]
    statements: list[Statement] = field(default_factory=list)
    rejected: list[tuple[Claim, str]] = field(default_factory=list)
    unanswered: list[str] = field(default_factory=list)

    async def speak(self, model_text: str) -> int:
        language = self.speech.identity.language
        for claim in _distinct(parse_claims(model_text)):
            verified = verify_claim(claim, self.evidence, language=language)
            if not verified.supported:
                self._reject(claim, verified.support.reason)
                continue
            await self._answer(verified)
        self._drop_model_text(model_text)
        voiced = sum(statement.voiced for statement in self.statements)
        if self.unanswered or self.rejected or not self.statements:
            reasons = [*self.unanswered, *(reason for _, reason in self.rejected)]
            await self.speech.abstain(
                self.evidence, self.needs, reason=reasons[0] if reasons else "claim_undeclared"
            )
        return voiced

    async def _answer(self, verified: VerifiedClaim) -> None:
        language = self.speech.identity.language
        if self.evidence is not None and self.evidence.scope == "historical":
            await self._fall_back(verified, HISTORICAL_GATHER, None)
            return
        current = render(verified, "current", language)
        if current is None:
            reason = RENDER_UNSUPPORTED if verified.claim.source == "screen" else NO_CURRENT_PATTERN
            await self._fall_back(verified, reason, None)
            return
        statement = await self._state(current, verified, None)
        if statement is None:
            self.unanswered.append(SYNTHESIS_UNAVAILABLE)
            return
        prevented = prevented_reason(self.speech.outcome(statement.publication_id))
        if prevented is not None:
            await self._fall_back(verified, prevented, statement.publication_id)

    async def _fall_back(self, verified: VerifiedClaim, reason: str, replaces: str | None) -> None:
        historical = (
            render(verified, "historical", self.speech.identity.language)
            if reason in HISTORICAL_REASONS
            else None
        )
        if historical is None:
            self.unanswered.append(reason)
            return
        statement = await self._state(historical, verified, replaces)
        if statement is None:
            self.unanswered.append(SYNTHESIS_UNAVAILABLE)
        elif not statement.voiced:
            refused = prevented_reason(self.speech.outcome(statement.publication_id))
            if refused is not None:
                self.unanswered.append(refused)

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

    async def _state(
        self, rendered: Rendered, verified: VerifiedClaim, replaces: str | None
    ) -> Statement | None:
        speech = self.speech
        look = verified.evidence
        if look is None or self.evidence is None:
            return None
        ready = await speech.prepare(
            rendered.text, rendered.rendering.language, purpose=rendered.rendering.tense
        )
        if ready is None:
            return None
        prepared, index = ready
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
            replaces=replaces,
        )
        speech.journal.record(
            "visual_statement_selected",
            source="agent",
            transcript_id=speech.identity.transcript_id,
            publication_id=binding.publication_id,
            replaces_publication_id=replaces,
            pattern_id=rendered.rendering.pattern_id,
            pattern_version=rendered.rendering.version,
            language=rendered.rendering.language,
            evidence_kind=look.kind,
            fields=[(span.name, span.start, span.end) for span in rendered.rendering.fields],
        )
        voiced = await speech.publish(prepared, binding)
        statement = Statement(rendered, verified, binding.publication_id, voiced, replaces)
        self.statements.append(statement)
        return statement
