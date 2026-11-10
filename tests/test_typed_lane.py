from datetime import UTC, datetime
from typing import Any, cast, get_args

import pytest
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode

from fakeyou.debrief.debrief_schema import DebriefItem
from fakeyou.memory.evidence import typed_first
from fakeyou.memory.ontology import PACK_VERSION, TYPED_EDGE_TYPES, edge_type_for
from fakeyou.memory.typed_lane import (
    TypedAssertion,
    TypedEvidence,
    assertions_from_edges,
    build_assertions,
    typed_episode_uuid,
)

AT = datetime(2026, 10, 1, 9, tzinfo=UTC)


def assertion(**overrides: Any) -> TypedAssertion:
    values: dict[str, Any] = {
        "kind": "decision_claim",
        "text": "The Orchid beta launch moved to 14 October 2026",
        "status": "stated",
        "evidence": (TypedEvidence("entry-1", "the launch moved to 14 October"),),
    }
    values.update(overrides)
    return TypedAssertion(**values)


def test_every_debrief_kind_has_exactly_one_namespaced_edge_type() -> None:
    kinds = get_args(DebriefItem.model_fields["kind"].annotation)

    assert {edge_type_for(kind) for kind in kinds} == set(TYPED_EDGE_TYPES)
    assert all(name.startswith("Core") for name in TYPED_EDGE_TYPES)
    assert PACK_VERSION == "core/v1"


def test_a_typed_assertion_becomes_a_typed_edge_with_its_evidence() -> None:
    episode, nodes, edges = build_assertions(
        group_id="team_alpha", meeting_id="m1", at=AT, items=[assertion()]
    )

    [edge] = edges
    assert edge.episodes == [episode.uuid]
    assert edge.name == "CoreDecisionClaim"
    assert edge.fact == "The Orchid beta launch moved to 14 October 2026"
    assert edge.group_id == "team_alpha"
    assert edge.valid_at == AT
    assert edge.attributes["pack"] == PACK_VERSION
    assert edge.attributes["status"] == "stated"
    assert edge.attributes["modality"] == "participant_claimed"
    assert edge.attributes["evidence_quotes"] == "the launch moved to 14 October"
    assert edge.attributes["evidence_entry_ids"] == "entry-1"
    assert {node.uuid for node in nodes} >= {edge.source_node_uuid, edge.target_node_uuid}


def test_building_twice_yields_the_same_ids_so_reingest_is_idempotent() -> None:
    first = build_assertions(group_id="g", meeting_id="m1", at=AT, items=[assertion()])
    second = build_assertions(group_id="g", meeting_id="m1", at=AT, items=[assertion()])

    assert first[0].uuid == second[0].uuid
    assert [edge.uuid for edge in first[2]] == [edge.uuid for edge in second[2]]
    assert [node.uuid for node in first[1]] == [node.uuid for node in second[1]]


def _edge(name: str, fact: str) -> EntityEdge:
    return EntityEdge(
        source_node_uuid="a",
        target_node_uuid="b",
        name=name,
        fact=fact,
        group_id="g",
        created_at=AT,
        valid_at=AT,
        reference_time=AT,
    )


def test_debrief_items_map_to_typed_assertions_with_their_evidence() -> None:
    from fakeyou.debrief.debrief_schema import DebriefItem, EvidenceRef
    from fakeyou.memory.typed_lane import assertions_from_items

    items = [
        DebriefItem(
            kind="action_item",
            text="Bima sends the load-test report",
            status="stated",
            evidence=(EvidenceRef(entry_id="e7", quote="I will send the load-test report"),),
            assignee_claim="Bima",
            due_text="Friday",
        )
    ]

    [mapped] = assertions_from_items(items)

    assert mapped.kind == "action_item"
    assert mapped.assignee == "Bima"
    assert mapped.due_text == "Friday"
    assert mapped.evidence[0].entry_id == "e7"


async def test_write_typed_assertions_saves_nodes_then_edges_with_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fakeyou.memory.typed_lane import write_typed_assertions

    saved: list[str] = []

    async def node_save(self: EntityNode, _driver: Any) -> None:
        saved.append(f"node:{self.uuid}")

    async def edge_save(self: EntityEdge, _driver: Any) -> None:
        saved.append(f"edge:{self.name}")

    async def node_embedding(self: EntityNode, _embedder: Any) -> None:
        self.name_embedding = [0.1]

    async def edge_embedding(self: EntityEdge, _embedder: Any) -> None:
        self.fact_embedding = [0.2]

    monkeypatch.setattr(EntityNode, "save", node_save)
    monkeypatch.setattr(EpisodicNode, "save", node_save)
    monkeypatch.setattr(EntityNode, "generate_name_embedding", node_embedding)
    monkeypatch.setattr(EntityEdge, "save", edge_save)
    monkeypatch.setattr(EntityEdge, "generate_embedding", edge_embedding)
    store = cast(Graphiti, SimpleNamespace(driver=object(), embedder=object()))

    written = await write_typed_assertions(
        store, group_id="g", meeting_id="m1", at=AT, items=[assertion()]
    )

    assert len(written) == 1
    assert saved[-1] == "edge:CoreDecisionClaim"
    assert len([entry for entry in saved if entry.startswith("node:")]) == 3


def test_typed_edges_rank_before_untyped_edges_without_reordering_within_a_lane() -> None:
    untyped_one = _edge("RELATES_TO", "Budi cannot join the meeting")
    typed = _edge("CoreDecisionClaim", "The launch moved to 14 October 2026")
    untyped_two = _edge("MENTIONED", "Budi joined the meeting")

    assert typed_first([untyped_one, typed, untyped_two]) == [typed, untyped_one, untyped_two]


def test_assertions_roundtrip_through_the_typed_episode_edges() -> None:
    item = assertion(
        evidence=(
            TypedEvidence("entry-1", "the launch moved to 14 October"),
            TypedEvidence("entry-2", "not 21 October"),
        ),
        assignee="maya",
        due_text="friday",
    )
    episode, _nodes, edges = build_assertions(
        group_id="team_alpha", meeting_id="m1", at=AT, items=[item]
    )

    assert episode.uuid == typed_episode_uuid("team_alpha", "m1")
    assert assertions_from_edges(edges, episode.uuid) == (item,)
    assert assertions_from_edges(edges, typed_episode_uuid("team_alpha", "m2")) == ()


def test_edges_from_other_packs_or_untyped_names_are_not_assertions() -> None:
    _episode, _nodes, edges = build_assertions(
        group_id="g", meeting_id="m1", at=AT, items=[assertion()]
    )
    [edge] = edges
    foreign = edge.model_copy(update={"attributes": {**edge.attributes, "pack": "other/v9"}})
    untyped = edge.model_copy(update={"name": "RELATES_TO"})

    assert assertions_from_edges([foreign, untyped], edge.episodes[0]) == ()
