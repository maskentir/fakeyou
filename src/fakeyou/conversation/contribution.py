from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from fakeyou.foundation.provenance import OWNER_SPEAKER_IDENTITY, UNVERIFIED_SPEAKER_IDENTITY
from fakeyou.session.facts import Confidence, Disclosure, OwnerModelKind

ContributionSource = Literal["brief", "memory", "owner_model"]


@dataclass(frozen=True, slots=True)
class OwnerModelSource:
    text: str
    disclosure: Disclosure
    kind: OwnerModelKind = "fact"
    confidence: Confidence = "firm"
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecalledFact:
    text: str
    times: tuple[datetime, ...] = ()
    owner_spoken: bool = False


class _EvidenceSource(BaseModel):
    source_at: datetime | None = None
    recorded_at: datetime | None = None
    speaker_verified: bool = False
    speaker_identity: str = UNVERIFIED_SPEAKER_IDENTITY

    @property
    def owner_spoken(self) -> bool:
        return self.speaker_verified and self.speaker_identity == OWNER_SPEAKER_IDENTITY


class _EvidenceFact(BaseModel):
    fact_ids: list[str]
    text: str
    sources: list[_EvidenceSource] = []

    def recalled(self) -> RecalledFact:
        moments = (
            moment
            for source in self.sources
            for moment in (source.source_at, source.recorded_at)
            if moment is not None
        )
        return RecalledFact(
            self.text,
            tuple(dict.fromkeys(moments)),
            owner_spoken=bool(self.sources) and all(source.owner_spoken for source in self.sources),
        )


class _Evidence(BaseModel):
    facts: list[_EvidenceFact] = []


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    facts: Mapping[str, str] = field(default_factory=dict)
    questions: Mapping[str, str] = field(default_factory=dict)
    objectives: Mapping[str, str] = field(default_factory=dict)
    owner_model: Mapping[str, OwnerModelSource] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.facts or self.questions or self.objectives or self.owner_model)

    def items(self) -> tuple[tuple[ContributionSource, str, str], ...]:
        return (
            *(("brief", ref, text) for ref, text in self.facts.items()),
            *(("brief", ref, text) for ref, text in self.questions.items()),
            *(("brief", ref, text) for ref, text in self.objectives.items()),
            *(
                ("owner_model", ref, source.text)
                for ref, source in self.owner_model.items()
                if source.disclosure == "room_speakable"
            ),
        )


def source_registry(
    *,
    brief_facts: Mapping[str, str] | None = None,
    brief_questions: Mapping[str, str] | None = None,
    brief_objectives: Mapping[str, str] | None = None,
    owner_model: Mapping[str, OwnerModelSource] | None = None,
) -> SourceRegistry:
    return SourceRegistry(
        facts=dict(brief_facts or {}),
        questions=dict(brief_questions or {}),
        objectives=dict(brief_objectives or {}),
        owner_model=dict(owner_model or {}),
    )


def memory_refs_from_evidence(text: str) -> dict[str, RecalledFact]:
    try:
        evidence = _Evidence.model_validate_json(text)
    except ValidationError:
        return {}
    return {
        fact_id: fact.recalled() for fact in evidence.facts for fact_id in fact.fact_ids if fact_id
    }
