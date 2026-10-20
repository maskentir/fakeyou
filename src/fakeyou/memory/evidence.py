import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from pydantic import TypeAdapter

from fakeyou.config.settings import RecallSettings
from fakeyou.foundation.provenance import (
    EVIDENCE_SCHEMA,
    OWNER_SPEAKER_IDENTITY,
    UNVERIFIED_SPEAKER_IDENTITY,
    EvidenceOutcome,
    ExcludedDict,
    FactDict,
    FactEvidence,
    MemoryEvidence,
    SourceEvidence,
    SourceKind,
    render_evidence,
)
from fakeyou.memory.owner_notes import OWNER_NOTE_SOURCE, NoteStatus, read_note_states
from fakeyou.memory.persona import memory_group_for
from fakeyou.observability.faults import capture_error

TRANSCRIPT_SOURCE_SUFFIX = "transcript"
SCREEN_SOURCE_SUFFIX = "shared screen"
CHAT_SOURCE_SUFFIX = "chat"

RECALL_EMPTY_RESULT = "No relevant meeting memory found."
PREFETCH_EMPTY_EVIDENCE = (
    "Memory evidence: memory holds nothing that matches the participant message above. If the "
    "answer depends on memory, say it is not in your notes; do not guess."
)
PREFETCH_UNAVAILABLE_EVIDENCE = (
    "Memory evidence: memory search was unavailable for the participant message above. Treat any "
    "detail that depends on memory as unknown; do not guess."
)
PREFETCH_ADDITIONAL_EMPTY_EVIDENCE = (
    "Memory evidence: memory holds no additional matches for the participant message above. "
    "Earlier evidence in this conversation still stands; use it when it answers the question, "
    "otherwise say the detail is not in your notes; do not guess."
)
RENDERED_EVIDENCE_SETS = 3
CURRENT_FACTS_FILTER = SearchFilters(
    invalid_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)]],
    expired_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)]],
)

SearchBackend = Literal["search", "search_recipe"]


class _RenderedEvidence(TypedDict):
    schema: str
    facts: list[FactDict]
    excluded: ExcludedDict


_RENDERED_EVIDENCE = TypeAdapter(_RenderedEvidence)


@dataclass(frozen=True, slots=True)
class EvidenceResult:
    outcome: EvidenceOutcome
    facts: tuple[FactEvidence, ...]
    excluded: dict[str, int]
    search_rank: dict[str, int]


class NoteStateReader(Protocol):
    async def __call__(
        self, memory: Graphiti, *, group_id: str, episode_uuids: list[str]
    ) -> Mapping[str, NoteStatus]: ...


class MemorySearch(Protocol):
    async def __call__(self, query: str, *, owner_id: str, twin_id: str) -> MemoryEvidence: ...


def invalidated(edge: EntityEdge) -> bool:
    return edge.invalid_at is not None or edge.expired_at is not None


def search_recipe(recall: RecallSettings) -> SearchConfig:
    return SearchConfig(
        edge_config=EdgeSearchConfig(
            search_methods=[EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
            reranker=EdgeReranker.cross_encoder,
        ),
        limit=recall.search_limit,
    )


async def _recipe_edges(
    memory: Graphiti, query: str, *, group_id: str, recall: RecallSettings
) -> tuple[list[EntityEdge], list[EntityEdge]]:
    current, everything = await asyncio.gather(
        memory.search_(
            query,
            config=search_recipe(recall),
            group_ids=[group_id],
            search_filter=CURRENT_FACTS_FILTER,
        ),
        memory.search_(
            query, config=search_recipe(recall), group_ids=[group_id], search_filter=None
        ),
    )
    return current.edges, everything.edges


async def _search_pair(
    memory: Graphiti, query: str, *, group_id: str, recall: RecallSettings
) -> tuple[list[EntityEdge], list[EntityEdge]]:
    current, everything = await asyncio.gather(
        memory.search(
            query,
            group_ids=[group_id],
            num_results=recall.search_limit,
            search_filter=CURRENT_FACTS_FILTER,
        ),
        memory.search(query, group_ids=[group_id], num_results=recall.search_limit),
    )
    return current, everything


async def search_edges(
    memory: Graphiti,
    query: str,
    *,
    group_id: str,
    recall: RecallSettings,
    backend: SearchBackend = "search",
) -> list[EntityEdge]:
    pair = _recipe_edges if backend == "search_recipe" else _search_pair
    async with asyncio.timeout(recall.timeout_seconds):
        current, everything = await pair(memory, query, group_id=group_id, recall=recall)
    seen = {edge.uuid for edge in current}
    return [
        *current,
        *(edge for edge in everything if edge.uuid not in seen and invalidated(edge)),
    ]


def _evidence_kind(source_description: str) -> SourceKind:
    if source_description == OWNER_NOTE_SOURCE:
        return "owner_note"
    if source_description.endswith(TRANSCRIPT_SOURCE_SUFFIX):
        return "meeting_transcript"
    if source_description.endswith(SCREEN_SOURCE_SUFFIX):
        return "meeting_screen"
    if source_description.endswith(CHAT_SOURCE_SUFFIX):
        return "meeting_chat"
    return "other"


def _source_evidence(episode: EpisodicNode) -> SourceEvidence:
    kind = _evidence_kind(episode.source_description)
    return SourceEvidence(
        episode_id=episode.uuid,
        kind=kind,
        label=episode.source_description,
        source_at=episode.valid_at,
        recorded_at=episode.created_at,
        time_granularity="document" if kind in ("owner_note", "other") else "episode_window",
        speaker_identity=OWNER_SPEAKER_IDENTITY
        if kind == "owner_note"
        else UNVERIFIED_SPEAKER_IDENTITY,
    )


async def collect_facts(
    memory: Graphiti,
    edges: list[EntityEdge],
    *,
    group_id: str,
    note_states: NoteStateReader,
) -> EvidenceResult:
    episode_uuids = sorted({uuid for edge in edges for uuid in edge.episodes})
    episodes = (
        {
            episode.uuid: episode
            for episode in await EpisodicNode.get_by_uuids(memory.driver, episode_uuids)
        }
        if episode_uuids
        else {}
    )
    owned = sorted(
        uuid
        for uuid, episode in episodes.items()
        if episode.group_id == group_id and episode.source_description == OWNER_NOTE_SOURCE
    )
    states = await note_states(memory, group_id=group_id, episode_uuids=owned) if owned else {}
    facts: list[FactEvidence] = []
    excluded = {"pending": 0, "superseded": 0, "missing_source": 0}
    search_rank: dict[str, int] = {}
    for position, edge in enumerate(edges):
        key = " ".join(edge.fact.split())
        if key not in search_rank:
            search_rank[key] = position
        if edge.group_id != group_id:
            excluded["missing_source"] += 1
            continue
        present = [
            episodes[uuid]
            for uuid in edge.episodes
            if uuid in episodes and episodes[uuid].group_id == group_id
        ]
        notes = [episode for episode in present if episode.source_description == OWNER_NOTE_SOURCE]
        if any(states.get(note.uuid) == "pending" for note in notes):
            excluded["pending"] += 1
            continue
        superseded = [note for note in notes if states.get(note.uuid) == "superseded"]
        active_notes = [note for note in notes if states.get(note.uuid) in (None, "active")]
        if (superseded or invalidated(edge)) and not active_notes:
            excluded["superseded"] += 1
            continue
        usable = [episode for episode in present if states.get(episode.uuid) in (None, "active")]
        if not usable:
            excluded["missing_source"] += 1
            continue
        facts.append(
            FactEvidence(
                fact_ids=(edge.uuid,),
                text=edge.fact,
                sources=tuple(_source_evidence(episode) for episode in usable),
            )
        )
    return EvidenceResult(
        outcome="found" if facts else "empty",
        facts=tuple(facts),
        excluded=excluded,
        search_rank=search_rank,
    )


async def search_evidence(
    memory: Graphiti,
    query: str,
    *,
    group_id: str,
    recall: RecallSettings,
    backend: SearchBackend = "search",
) -> EvidenceResult:
    edges = await search_edges(memory, query, group_id=group_id, recall=recall, backend=backend)
    return await collect_facts(memory, edges, group_id=group_id, note_states=read_note_states)


def evidence_set(result: EvidenceResult, *, result_limit: int) -> MemoryEvidence:
    rendered = _RENDERED_EVIDENCE.validate_json(
        render_evidence(
            list(result.facts),
            result_limit=result_limit,
            excluded=result.excluded,
            compact=False,
            search_rank=result.search_rank,
        )
    )
    return MemoryEvidence(
        outcome="found" if rendered["facts"] else "empty",
        facts=rendered["facts"],
        excluded=rendered["excluded"],
    )


def unavailable_set() -> MemoryEvidence:
    return MemoryEvidence(
        outcome="unavailable",
        facts=[],
        excluded=ExcludedDict(pending=0, superseded=0, missing_source=0),
    )


def create_memory_search(memory: Graphiti, *, recall: RecallSettings) -> MemorySearch:
    async def search(query: str, *, owner_id: str, twin_id: str) -> MemoryEvidence:
        group_id = memory_group_for(owner_id, twin_id)
        result, _error = await capture_error(
            lambda: search_evidence(memory, query, group_id=group_id, recall=recall)
        )
        if result is None:
            return unavailable_set()
        return evidence_set(result, result_limit=recall.result_limit)

    return search


def _found_line(evidence: MemoryEvidence, facts: Sequence[FactDict]) -> str:
    return json.dumps(
        {"schema": EVIDENCE_SCHEMA, "facts": list(facts), "excluded": evidence["excluded"]},
        ensure_ascii=False,
    )


def _cut_text(evidence: MemoryEvidence, fact: FactDict, char_budget: int) -> str | None:
    text = fact["text"]
    keep = len(text)
    while keep > 0:
        cut = FactDict(fact_ids=fact["fact_ids"], text=text[:keep], sources=fact["sources"])
        line = _found_line(evidence, [cut])
        excess = len(line) - char_budget
        if excess <= 0:
            return line
        keep -= excess
    return None


def _trimmed_line(evidence: MemoryEvidence, char_budget: int) -> str | None:
    facts = list(evidence["facts"])
    while facts:
        line = _found_line(evidence, facts)
        if len(line) <= char_budget:
            return line
        if len(facts) == 1:
            return _cut_text(evidence, facts[0], char_budget)
        facts.pop()
    return None


def render_memory_evidence(evidence: Sequence[MemoryEvidence], char_budget: int) -> str:
    recent = list(evidence)[-RENDERED_EVIDENCE_SETS:]
    if not recent:
        return ""
    *older, latest = recent
    lines: list[str] = []
    for item in older:
        if item["outcome"] != "found" or not item["facts"]:
            continue
        line = _trimmed_line(item, char_budget)
        if line is not None:
            lines.append(line)
    if latest["outcome"] == "found" and latest["facts"]:
        lines.append(_found_line(latest, latest["facts"]))
    elif latest["outcome"] == "unavailable":
        lines.append(PREFETCH_UNAVAILABLE_EVIDENCE)
    else:
        lines.append(PREFETCH_ADDITIONAL_EMPTY_EVIDENCE if lines else PREFETCH_EMPTY_EVIDENCE)
    return "\n".join(lines)
