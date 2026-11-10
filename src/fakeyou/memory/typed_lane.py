from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import NAMESPACE_URL, uuid5

from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from fakeyou.debrief.debrief_schema import DebriefItem
from fakeyou.memory.ontology import (
    ASSERTION_LABEL,
    MEETING_LABEL,
    PACK_VERSION,
    AssertionKind,
    edge_type_for,
    kind_for_edge_type,
)

AssertionStatus = Literal["stated", "proposed", "needs_owner", "uncertain"]
EVIDENCE_SEPARATOR = " | "
NODE_NAME_MAX_CHARS = 120


@dataclass(frozen=True, slots=True)
class TypedEvidence:
    entry_id: str
    quote: str


@dataclass(frozen=True, slots=True)
class TypedAssertion:
    kind: AssertionKind
    text: str
    status: AssertionStatus
    evidence: tuple[TypedEvidence, ...]
    assignee: str | None = None
    due_text: str | None = None


def assertions_from_items(items: Iterable[DebriefItem]) -> list[TypedAssertion]:
    return [
        TypedAssertion(
            kind=item.kind,
            text=item.text,
            status=item.status,
            evidence=tuple(TypedEvidence(ref.entry_id, ref.quote) for ref in item.evidence),
            assignee=item.assignee_claim,
            due_text=item.due_text,
        )
        for item in items
    ]


def _stable_uuid(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "fakeyou:typed:" + ":".join(parts)))


def typed_episode_uuid(group_id: str, meeting_id: str) -> str:
    return _stable_uuid(group_id, "typed-episode", meeting_id)


def assertions_from_edges(
    edges: Iterable[EntityEdge], episode_uuid: str
) -> tuple[TypedAssertion, ...]:
    found: list[TypedAssertion] = []
    for edge in edges:
        if episode_uuid not in edge.episodes:
            continue
        kind = kind_for_edge_type(edge.name)
        attributes = edge.attributes
        if kind is None or attributes.get("pack") != PACK_VERSION:
            continue
        quotes = str(attributes.get("evidence_quotes") or "").split(EVIDENCE_SEPARATOR)
        entry_ids = str(attributes.get("evidence_entry_ids") or "").split(EVIDENCE_SEPARATOR)
        found.append(
            TypedAssertion(
                kind=kind,
                text=edge.fact,
                status=cast(AssertionStatus, str(attributes.get("status") or "stated")),
                evidence=tuple(
                    TypedEvidence(entry_id=entry_id, quote=quote)
                    for entry_id, quote in zip(entry_ids, quotes, strict=False)
                    if quote
                ),
                assignee=str(attributes.get("assignee") or "") or None,
                due_text=str(attributes.get("due_text") or "") or None,
            )
        )
    return tuple(found)


def build_assertions(
    *,
    group_id: str,
    meeting_id: str,
    at: datetime,
    items: Sequence[TypedAssertion],
) -> tuple[EpisodicNode, list[EntityNode], list[EntityEdge]]:
    episode = EpisodicNode(
        uuid=typed_episode_uuid(group_id, meeting_id),
        name=f"{meeting_id}-typed-assertions",
        group_id=group_id,
        created_at=at,
        valid_at=at,
        source=EpisodeType.json,
        source_description=f"Google Meet {meeting_id} debrief assertions",
        content="\n".join(item.text for item in items),
    )
    meeting = EntityNode(
        uuid=_stable_uuid(group_id, "meeting", meeting_id),
        name=meeting_id,
        group_id=group_id,
        labels=["Entity", MEETING_LABEL],
        created_at=at,
        attributes={"pack": PACK_VERSION},
    )
    nodes: list[EntityNode] = [meeting]
    edges: list[EntityEdge] = []
    for item in items:
        node = EntityNode(
            uuid=_stable_uuid(group_id, meeting_id, item.kind, item.text),
            name=item.text[:NODE_NAME_MAX_CHARS],
            group_id=group_id,
            labels=["Entity", ASSERTION_LABEL],
            created_at=at,
            attributes={"pack": PACK_VERSION, "kind": item.kind},
        )
        nodes.append(node)
        edges.append(
            EntityEdge(
                uuid=_stable_uuid(group_id, meeting_id, "edge", item.kind, item.text),
                group_id=group_id,
                source_node_uuid=meeting.uuid,
                target_node_uuid=node.uuid,
                created_at=at,
                name=edge_type_for(item.kind),
                fact=item.text,
                episodes=[episode.uuid],
                valid_at=at,
                reference_time=at,
                attributes={
                    "pack": PACK_VERSION,
                    "status": item.status,
                    "modality": "participant_claimed",
                    "disclosure": "room_speakable",
                    "evidence_quotes": EVIDENCE_SEPARATOR.join(ref.quote for ref in item.evidence),
                    "evidence_entry_ids": EVIDENCE_SEPARATOR.join(
                        ref.entry_id for ref in item.evidence
                    ),
                    "assignee": item.assignee or "",
                    "due_text": item.due_text or "",
                },
            )
        )
    return episode, nodes, edges


async def write_typed_assertions(
    store: Graphiti,
    *,
    group_id: str,
    meeting_id: str,
    at: datetime,
    items: Sequence[TypedAssertion],
) -> tuple[str, ...]:
    episode, nodes, edges = build_assertions(
        group_id=group_id, meeting_id=meeting_id, at=at, items=items
    )
    await episode.save(store.driver)
    for node in nodes:
        await node.generate_name_embedding(store.embedder)
        await node.save(store.driver)
    for edge in edges:
        await edge.generate_embedding(store.embedder)
        await edge.save(store.driver)
    return tuple(edge.uuid for edge in edges)
