import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from fakeyou.session.facts import Disclosure

ContributionSource = Literal["profile", "brief", "memory", "owner_model"]


@dataclass(frozen=True, slots=True)
class OwnerModelSource:
    text: str
    disclosure: Disclosure


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


def memory_refs_from_evidence(text: str) -> dict[str, str]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    refs: dict[str, str] = {}
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return refs
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        fact_text = fact.get("text")
        fact_ids = fact.get("fact_ids")
        if not isinstance(fact_text, str) or not isinstance(fact_ids, list):
            continue
        for fact_id in fact_ids:
            if isinstance(fact_id, str) and fact_id:
                refs[fact_id] = fact_text
    return refs
