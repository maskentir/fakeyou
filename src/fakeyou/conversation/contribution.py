from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from fakeyou.session.facts import Disclosure

ContributionSource = Literal["profile", "brief", "memory", "owner_model"]


@dataclass(frozen=True, slots=True)
class OwnerModelSource:
    text: str
    disclosure: Disclosure
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecalledFact:
    text: str
    times: tuple[datetime, ...] = ()


class _EvidenceSource(BaseModel):
    source_at: datetime | None = None
    recorded_at: datetime | None = None


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
        return RecalledFact(self.text, tuple(dict.fromkeys(moments)))


class _Evidence(BaseModel):
    facts: list[_EvidenceFact] = []


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    facts: Mapping[str, str] = field(default_factory=dict)
    questions: Mapping[str, str] = field(default_factory=dict)
    objectives: Mapping[str, str] = field(default_factory=dict)
    profile: Mapping[str, str] = field(default_factory=dict)
    owner_model: Mapping[str, OwnerModelSource] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (
            self.facts or self.questions or self.objectives or self.profile or self.owner_model
        )

    def items(self) -> tuple[tuple[ContributionSource, str, str], ...]:
        return (
            *(("brief", ref, text) for ref, text in self.facts.items()),
            *(("brief", ref, text) for ref, text in self.questions.items()),
            *(("brief", ref, text) for ref, text in self.objectives.items()),
            *(("profile", ref, text) for ref, text in self.profile.items()),
            *(
                ("owner_model", ref, source.text)
                for ref, source in self.owner_model.items()
                if source.disclosure == "room_speakable"
            ),
        )


def source_registry(
    *,
    profile_facts: Sequence[str] = (),
    brief_facts: Mapping[str, str] | None = None,
    brief_questions: Mapping[str, str] | None = None,
    brief_objectives: Mapping[str, str] | None = None,
    owner_model: Mapping[str, OwnerModelSource] | None = None,
) -> SourceRegistry:
    return SourceRegistry(
        facts=dict(brief_facts or {}),
        questions=dict(brief_questions or {}),
        objectives=dict(brief_objectives or {}),
        profile={f"fact-{index}": text for index, text in enumerate(profile_facts)},
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
