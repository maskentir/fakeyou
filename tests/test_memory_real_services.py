from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from graphiti_core import Graphiti
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from neo4j.exceptions import Neo4jError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import RecallSettings
from fakeyou.memory.evidence import evidence_set, search_evidence
from fakeyou.memory.ingestion import (
    IngestScope,
    MeetingScope,
    correction_episode_name,
    episode_name,
    ingest_corrections,
    ingest_meeting,
)
from fakeyou.memory.memory import meeting_episodes, meeting_saga, open_graphiti_memory
from fakeyou.memory.owner_notes import (
    INTENT_CREATE_QUERY,
    OWNER_NOTE_SOURCE,
    ensure_note_indexes,
)
from fakeyou.persistence.ingestion_status import IngestionStatus, status_path, write_status
from fakeyou.persistence.record import load_meeting_record
from tests._error_support import message_pattern
from tests._ingestion_support import correction_record, said
from tests._memory_fakes import make_real_graphiti
from tests._memory_support import memory_settings

GROUP = "c10_group"
WINDOW_AT = datetime(2026, 10, 16, 9, tzinfo=UTC)


def unique_group() -> str:
    return f"{GROUP}_{uuid4().hex[:10]}"


async def add_window_episode(memory: Graphiti, group: str, meeting: str, index: int) -> str:
    result = await memory.add_episode(
        name=episode_name(meeting, index),
        episode_body=f"host: c10 window {index} budget review notes",
        source_description=f"Google Meet {meeting} transcript",
        reference_time=WINDOW_AT + timedelta(minutes=index),
        source=EpisodeType.message,
        group_id=group,
        saga=meeting_saga(meeting),
    )
    return str(result.episode.uuid)


async def add_fact_edge(
    memory: Graphiti,
    group: str,
    episode_uuid: str,
    fact: str,
    *,
    invalid_at: datetime | None,
) -> str:
    source = EntityNode(name=f"Budget owner {uuid4().hex[:6]}", group_id=group, labels=["Entity"])
    target = EntityNode(name=f"Q3 budget {uuid4().hex[:6]}", group_id=group, labels=["Entity"])
    for node in (source, target):
        await node.generate_name_embedding(memory.embedder)
        await node.save(memory.driver)
    edge = EntityEdge(
        group_id=group,
        source_node_uuid=source.uuid,
        target_node_uuid=target.uuid,
        created_at=WINDOW_AT,
        name="DECIDED",
        fact=fact,
        episodes=[episode_uuid],
        valid_at=WINDOW_AT,
        invalid_at=invalid_at,
    )
    await edge.generate_embedding(memory.embedder)
    await edge.save(memory.driver)
    episode = await EpisodicNode.get_by_uuid(memory.driver, episode_uuid)
    episode.entity_edges = [*episode.entity_edges, edge.uuid]
    await episode.save(memory.driver)
    return edge.uuid


def long_window(index: int) -> Any:
    return said(
        float(index * 61),
        f"Window {index} confirms the Thursday launch plan with the rollout checklist and "
        "the client demo schedule for all regions this week.",
    )


@pytest.mark.container
async def test_real_recall_honours_the_temporal_filter(real_graphiti: Graphiti) -> None:
    group = unique_group()
    episode = await add_window_episode(real_graphiti, group, "c10-temporal", 0)
    current = await add_fact_edge(
        real_graphiti, group, episode, "Budget approved for Q3", invalid_at=None
    )
    stale = await add_fact_edge(
        real_graphiti,
        group,
        episode,
        "Budget rejected for Q3",
        invalid_at=datetime(2026, 10, 1, tzinfo=UTC),
    )

    result = await search_evidence(
        real_graphiti, "Budget Q3", group_id=group, recall=RecallSettings()
    )
    rendered = evidence_set(result, result_limit=5)

    assert [fact["text"] for fact in rendered["facts"]] == ["Budget approved for Q3"]
    assert rendered["facts"][0]["fact_ids"] == [current]
    assert rendered["excluded"]["superseded"] == 1
    assert current != stale


@pytest.mark.container
async def test_real_owner_note_uuid_constraint_is_unique(real_graphiti: Graphiti) -> None:
    await ensure_note_indexes(real_graphiti)
    params = {
        "uuid": f"c10-note-{uuid4().hex}",
        "group_id": unique_group(),
        "digest": "d" * 64,
        "source_at": datetime(2026, 10, 16, 9, tzinfo=UTC),
        "recorded_at": datetime(2026, 10, 16, 9, tzinfo=UTC),
        "target_uuid": None,
    }
    await real_graphiti.driver.execute_query(INTENT_CREATE_QUERY, **params)

    with pytest.raises(Neo4jError, match="already exists"):
        await real_graphiti.driver.execute_query(INTENT_CREATE_QUERY, **params)


@pytest.mark.container
async def test_real_saga_resume_continues_after_the_last_window(
    real_graphiti: Graphiti, neo4j_endpoint: str
) -> None:
    group = unique_group()
    scope = IngestScope(group, "c10-resume", "Rani Twin")
    first = await ingest_meeting(real_graphiti, [long_window(index) for index in range(2)], scope)

    reopened = make_real_graphiti(neo4j_endpoint, password="harness-only")
    try:
        resumed = await ingest_meeting(reopened, [long_window(index) for index in range(4)], scope)
        episodes = await meeting_episodes(
            reopened, group_id=group, meeting_id="c10-resume", last_n=10
        )
    finally:
        await cast(Any, reopened).close()

    assert (first.windows, first.ingested, first.completed) == (2, 2, True)
    assert (resumed.resumed_from, resumed.ingested, resumed.completed) == (2, 2, True)
    assert sorted(resumed.episode_uuids) == [2, 3]
    assert sorted(episode.name for episode in episodes) == [
        episode_name("c10-resume", index) for index in range(4)
    ]


@pytest.mark.container
async def test_real_correction_invalidates_original_edges(
    real_graphiti: Graphiti, tmp_path: Path
) -> None:
    group = unique_group()
    record_path = correction_record(tmp_path)
    ingested = await ingest_meeting(
        real_graphiti,
        load_meeting_record(record_path),
        IngestScope(group, "meeting-1", "Rani Twin"),
    )
    original = ingested.episode_uuids[1]
    edge_uuid = await add_fact_edge(
        real_graphiti, group, original, "Harun owns the rollout checklist", invalid_at=None
    )
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="meeting-1",
            group_id=group,
            status="completed",
            windows=ingested.windows,
            ingested=ingested.ingested,
            episode_uuids=dict(ingested.episode_uuids),
        ),
    )

    corrected = await ingest_corrections(
        real_graphiti, record_path, MeetingScope(group, "meeting-1")
    )
    [edge] = await EntityEdge.get_by_uuids(real_graphiti.driver, [edge_uuid])
    episodes = await meeting_episodes(
        real_graphiti, group_id=group, meeting_id="meeting-1", last_n=10
    )

    assert corrected.completed is True
    assert corrected.changed_windows == 1
    assert edge.invalid_at is not None
    assert edge.expired_at is not None
    assert correction_episode_name("meeting-1", 1) in {episode.name for episode in episodes}


@pytest.mark.container
async def test_real_pending_owner_note_facts_are_invisible_to_recall(
    real_graphiti: Graphiti,
) -> None:
    group = unique_group()
    await ensure_note_indexes(real_graphiti)
    note_uuid = str(uuid4())
    await EpisodicNode(
        uuid=note_uuid,
        name=f"owner-note:{note_uuid}",
        group_id=group,
        labels=[],
        source=EpisodeType.text,
        content="The pending budget cap is forty million.",
        source_description=OWNER_NOTE_SOURCE,
        created_at=WINDOW_AT,
        valid_at=WINDOW_AT,
    ).save(real_graphiti.driver)
    await real_graphiti.driver.execute_query(
        INTENT_CREATE_QUERY,
        uuid=note_uuid,
        group_id=group,
        digest="e" * 64,
        source_at=WINDOW_AT,
        recorded_at=WINDOW_AT,
        target_uuid=None,
    )
    await add_fact_edge(
        real_graphiti, group, note_uuid, "Pending budget cap is forty million", invalid_at=None
    )

    result = await search_evidence(
        real_graphiti, "pending budget cap", group_id=group, recall=RecallSettings()
    )
    rendered = evidence_set(result, result_limit=5)

    assert rendered["facts"] == []
    assert rendered["outcome"] == "empty"
    assert rendered["excluded"]["pending"] == 1


@pytest.mark.container
async def test_embedding_dim_mismatch_is_rejected_before_connecting() -> None:
    settings = memory_settings()
    config = settings.model_copy(
        update={"embedder": settings.embedder.model_copy(update={"dim": 1024})}
    )

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.CONFIG_EMBEDDING_DIM_MISMATCH)
    ) as caught:
        async with open_graphiti_memory(config):
            pytest.fail("memory must not open with a mismatched embedding dimension")

    assert caught.value.code is ErrorCode.CONFIG_EMBEDDING_DIM_MISMATCH
