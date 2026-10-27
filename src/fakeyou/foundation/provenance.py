import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, TypedDict, get_args

EVIDENCE_SCHEMA = "memory_evidence/v1"
EvidenceOutcome = Literal["found", "empty", "unavailable"]
SourceKind = Literal["owner_note", "meeting_transcript", "meeting_screen", "meeting_chat", "other"]
TimeGranularity = Literal["document", "episode_window"]
SOURCE_KINDS: frozenset[str] = frozenset(get_args(SourceKind))
TIME_GRANULARITIES: frozenset[str] = frozenset(get_args(TimeGranularity))


class SourceDict(TypedDict):
    episode_id: str
    kind: SourceKind
    label: str
    source_at: str
    recorded_at: str
    time_granularity: TimeGranularity
    speaker_verified: bool
    speaker_identity: str


class FactDict(TypedDict):
    fact_ids: list[str]
    text: str
    sources: list[SourceDict]


class ExcludedDict(TypedDict):
    pending: int
    superseded: int
    missing_source: int


class MemoryEvidence(TypedDict):
    outcome: EvidenceOutcome
    facts: list[FactDict]
    excluded: ExcludedDict


OWNER_SPEAKER_IDENTITY = "owner"
UNVERIFIED_SPEAKER_IDENTITY = "unverified: may or may not be the owner or any named person"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    episode_id: str
    kind: SourceKind
    label: str
    source_at: datetime
    recorded_at: datetime
    time_granularity: TimeGranularity
    speaker_verified: bool = False
    speaker_identity: str = UNVERIFIED_SPEAKER_IDENTITY

    def __post_init__(self) -> None:
        if not self.episode_id.strip():
            raise ValueError("episode_id must not be blank")
        if self.kind not in SOURCE_KINDS:
            raise ValueError(f"kind must be one of {sorted(SOURCE_KINDS)}")
        if self.source_at.tzinfo is None or self.source_at.utcoffset() is None:
            raise ValueError("source_at must carry timezone")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must carry timezone")
        if self.time_granularity not in TIME_GRANULARITIES:
            raise ValueError("time_granularity must be document or episode_window")
        if not self.speaker_identity.strip():
            raise ValueError("speaker_identity must not be blank")


@dataclass(frozen=True, slots=True)
class FactEvidence:
    fact_ids: tuple[str, ...]
    text: str
    sources: tuple[SourceEvidence, ...]

    def __post_init__(self) -> None:
        if not self.fact_ids:
            raise ValueError("fact_ids must not be empty")
        if any(not item.strip() for item in self.fact_ids):
            raise ValueError("fact_ids must hold valid ids")
        if not self.text.strip():
            raise ValueError("text must not be blank")


def _rendered_source_compact(source: SourceEvidence) -> dict[str, str | bool]:
    rendered: dict[str, str | bool] = {
        "kind": source.kind,
        "label": source.label,
        "source_at": source.source_at.astimezone(UTC).isoformat(),
    }
    if source.recorded_at != source.source_at:
        rendered["recorded_at"] = source.recorded_at.astimezone(UTC).isoformat()
    rendered["speaker_identity"] = source.speaker_identity
    return rendered


_OrderedEntry = tuple[int, float, str, str, tuple[str, ...], tuple[SourceEvidence, ...]]


def _validate_render_inputs(result_limit: int, excluded: dict[str, int]) -> None:
    if result_limit < 1:
        raise ValueError("result_limit must be at least 1")
    allowed = ("pending", "superseded", "missing_source")
    for key in excluded:
        if key not in allowed:
            raise ValueError("excluded holds unknown key")
    for value in excluded.values():
        if value < 0:
            raise ValueError("excluded counts must be zero or more")


def _check_sources(facts: list[FactEvidence]) -> None:
    seen: dict[str, SourceEvidence] = {}
    for fact in facts:
        for source in fact.sources:
            prior = seen.get(source.episode_id)
            if prior is not None and prior != source:
                raise ValueError(f"conflicting metadata for episode {source.episode_id}")
            seen[source.episode_id] = source


def _merge_facts(
    facts: list[FactEvidence],
) -> tuple[dict[str, set[str]], dict[str, dict[str, SourceEvidence]]]:
    merged_ids: dict[str, set[str]] = {}
    merged_sources: dict[str, dict[str, SourceEvidence]] = {}
    for fact in facts:
        key = " ".join(fact.text.split())
        ids = merged_ids.setdefault(key, set())
        ids.update(fact.fact_ids)
        source_map = merged_sources.setdefault(key, {})
        for source in fact.sources:
            source_map[source.episode_id] = source
    return merged_ids, merged_sources


def _rank_facts(
    merged_ids: dict[str, set[str]], merged_sources: dict[str, dict[str, SourceEvidence]]
) -> list[_OrderedEntry]:
    ordered: list[_OrderedEntry] = []
    for key, ids in merged_ids.items():
        ranked = tuple(
            sorted(
                merged_sources[key].values(),
                key=lambda source: (
                    source.kind != "owner_note",
                    -source.source_at.timestamp(),
                    source.episode_id,
                ),
            )
        )
        fact_ids = tuple(sorted(ids))
        if ranked:
            owner_rank = 0 if any(item.kind == "owner_note" for item in ranked) else 1
            newest = max(item.source_at for item in ranked).timestamp()
            time_rank = -newest
        else:
            owner_rank = 1
            time_rank = float("inf")
        ordered.append((owner_rank, time_rank, fact_ids[0], key, fact_ids, ranked))
    return ordered


def _trim_ordered(
    ordered: list[_OrderedEntry], result_limit: int, search_rank: dict[str, int] | None
) -> list[_OrderedEntry]:
    if search_rank is None:
        ordered.sort(key=lambda entry: (entry[0], entry[1], entry[2], entry[3]))
        return ordered[:result_limit]
    ordered.sort(
        key=lambda entry: (
            search_rank.get(entry[3], len(search_rank)),
            entry[0],
            entry[1],
            entry[2],
            entry[3],
        )
    )
    trimmed = ordered[:result_limit]
    if trimmed and not any(entry[0] == 0 for entry in trimmed):
        pinned = next((entry for entry in ordered if entry[0] == 0), None)
        if pinned is not None:
            trimmed = [*trimmed[:-1], pinned]
    return trimmed


def _render_fact_entry(entry: _OrderedEntry, *, compact: bool) -> dict[str, object]:
    text = entry[3]
    fact_ids = entry[4]
    ranked = entry[5]
    rendered_sources: list[dict[str, str | bool]] = []
    for source in ranked:
        if compact:
            rendered_sources.append(_rendered_source_compact(source))
            continue
        rendered_source: dict[str, str | bool] = {
            "episode_id": source.episode_id,
            "kind": source.kind,
            "label": source.label,
            "source_at": source.source_at.astimezone(UTC).isoformat(),
            "recorded_at": source.recorded_at.astimezone(UTC).isoformat(),
            "time_granularity": source.time_granularity,
            "speaker_verified": source.speaker_verified,
            "speaker_identity": source.speaker_identity,
        }
        rendered_sources.append(rendered_source)
    return {"fact_ids": list(fact_ids), "text": text, "sources": rendered_sources}


def render_evidence(
    facts: list[FactEvidence],
    *,
    result_limit: int,
    excluded: dict[str, int],
    compact: bool = False,
    search_rank: dict[str, int] | None = None,
) -> str:
    _validate_render_inputs(result_limit, excluded)
    _check_sources(facts)
    merged_ids, merged_sources = _merge_facts(facts)
    ordered = _rank_facts(merged_ids, merged_sources)
    trimmed = _trim_ordered(ordered, result_limit, search_rank)
    rendered_facts = [_render_fact_entry(entry, compact=compact) for entry in trimmed]
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "facts": rendered_facts,
        "excluded": {
            "pending": excluded.get("pending", 0),
            "superseded": excluded.get("superseded", 0),
            "missing_source": excluded.get("missing_source", 0),
        },
    }
    return json.dumps(payload, ensure_ascii=False)
