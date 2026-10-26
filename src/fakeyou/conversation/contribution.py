import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, cast

ContributionKind = Literal["fact", "correction", "question"]
ContributionSource = Literal["profile", "brief", "memory"]
GateOutcome = Literal["pass", "drop", "n/a"]

CONTRIBUTION_KINDS: frozenset[str] = frozenset({"fact", "correction", "question"})
CONTRIBUTION_SOURCES: frozenset[str] = frozenset({"profile", "brief", "memory"})


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    brief: Mapping[str, str] = field(default_factory=dict)
    brief_questions: frozenset[str] = frozenset()
    profile: Mapping[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.brief and not self.profile

    def text_for(
        self, source: ContributionSource, ref: str, memory: Mapping[str, str]
    ) -> str | None:
        if source == "brief":
            return self.brief.get(ref)
        if source == "profile":
            return self.profile.get(ref)
        return memory.get(ref)

    def items(self) -> tuple[tuple[ContributionSource, str, str], ...]:
        return (
            *(("brief", ref, text) for ref, text in self.brief.items()),
            *(("profile", ref, text) for ref, text in self.profile.items()),
        )


def source_registry(
    *,
    profile_facts: Sequence[str] = (),
    brief_facts: Mapping[str, str] | None = None,
    brief_questions: Mapping[str, str] | None = None,
    brief_objectives: Mapping[str, str] | None = None,
) -> SourceRegistry:
    questions = dict(brief_questions or {})
    objectives = dict(brief_objectives or {})
    return SourceRegistry(
        brief={**dict(brief_facts or {}), **questions, **objectives},
        brief_questions=frozenset(questions) | frozenset(objectives),
        profile={f"fact-{index}": text for index, text in enumerate(profile_facts)},
    )


@dataclass(frozen=True, slots=True)
class ContributionGate:
    registry: SourceRegistry
    trigger_text: str
    memory_refs: Callable[[], Mapping[str, str]] = lambda: {}


@dataclass(frozen=True, slots=True)
class GateVerdict:
    passed: bool
    reason: str = ""
    kind: str | None = None
    source: str | None = None
    source_ref: str | None = None
    source_text: str | None = None


def _dropped(reason: str, kind: str | None, source: str | None, ref: str | None) -> GateVerdict:
    return GateVerdict(passed=False, reason=reason, kind=kind, source=source, source_ref=ref)


@dataclass(frozen=True, slots=True)
class ContributionClaim:
    structured: bool
    intent: str
    text: str
    kind: str | None = None
    source: str | None = None
    source_ref: str | None = None


def validate_contribution(claim: ContributionClaim, gate: ContributionGate) -> GateVerdict:
    kind, source, source_ref, text = claim.kind, claim.source, claim.source_ref, claim.text
    if claim.intent == "silent" or not text.strip():
        return GateVerdict(passed=True, kind=kind, source=source, source_ref=source_ref)
    if not claim.structured:
        return _dropped("unstructured", kind, source, source_ref)
    if kind not in CONTRIBUTION_KINDS or source not in CONTRIBUTION_SOURCES or not source_ref:
        return _dropped("missing_kind_source", kind, source, source_ref)
    assert kind is not None
    assert source is not None
    assert source_ref is not None
    source_text = gate.registry.text_for(
        cast(ContributionSource, source), source_ref, gate.memory_refs()
    )
    if source_text is None:
        return _dropped("unknown_source_ref", kind, source, source_ref)
    if kind == "correction" and source == "memory":
        return _dropped("kind_source_mismatch", kind, source, source_ref)
    if kind == "question" and (
        source != "brief" or source_ref not in gate.registry.brief_questions
    ):
        return _dropped("kind_source_mismatch", kind, source, source_ref)
    return GateVerdict(
        passed=True, kind=kind, source=source, source_ref=source_ref, source_text=source_text
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
