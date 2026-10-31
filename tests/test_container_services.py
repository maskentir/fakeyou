from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType
from sqlalchemy import text

from fakeyou.db.engine import create_engine_for_url
from fakeyou.memory.ingestion import episode_name
from fakeyou.memory.memory import meeting_episodes, meeting_saga
from tests._container_harness import NEO4J_IMAGE, POSTGRES_IMAGE
from tests._memory_fakes import make_real_graphiti

ROOT = Path(__file__).resolve().parent.parent


def test_harness_images_match_the_compose_pins() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert f"image: {POSTGRES_IMAGE}" in compose
    assert f"image: {NEO4J_IMAGE}" in compose


@pytest.mark.container
def test_harness_postgres_serves_the_migrated_fixture(migrated_database_url: str) -> None:
    engine = create_engine_for_url(
        migrated_database_url, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT 1")).scalar_one() == 1
    finally:
        engine.dispose()


HARNESS_GROUP = "harness_group_a"


async def _add_window(memory: Graphiti, meeting: str, index: int, previous_uuid: str | None) -> str:
    result = await memory.add_episode(
        name=episode_name(meeting, index),
        episode_body=f"host: harness window {index} budget review notes",
        source_description=f"Google Meet {meeting} transcript",
        reference_time=datetime(2026, 10, 16, 9, index, tzinfo=UTC),
        source=EpisodeType.message,
        group_id=HARNESS_GROUP,
        saga=meeting_saga(meeting),
        saga_previous_episode_uuid=previous_uuid,
    )
    return str(result.episode.uuid)


@pytest.mark.container
async def test_real_graphiti_persists_the_saga_chain_per_group(
    real_graphiti: Graphiti,
) -> None:
    first = await _add_window(real_graphiti, "harness-saga", 0, None)
    second = await _add_window(real_graphiti, "harness-saga", 1, first)

    episodes = await meeting_episodes(
        real_graphiti, group_id=HARNESS_GROUP, meeting_id="harness-saga", last_n=10
    )
    isolated = await meeting_episodes(
        real_graphiti, group_id="harness_group_b", meeting_id="harness-saga", last_n=10
    )

    assert {episode.uuid for episode in episodes} == {first, second}
    assert isolated == []


@pytest.mark.container
async def test_real_graphiti_episodes_survive_a_fresh_client(
    real_graphiti: Graphiti, neo4j_endpoint: str
) -> None:
    stored = await _add_window(real_graphiti, "harness-reopen", 0, None)

    reopened = make_real_graphiti(neo4j_endpoint, password="harness-only")
    try:
        episodes = await meeting_episodes(
            reopened, group_id=HARNESS_GROUP, meeting_id="harness-reopen", last_n=10
        )
    finally:
        await cast(Any, reopened).close()

    assert stored in {episode.uuid for episode in episodes}


@pytest.mark.container
async def test_redis_stream_group_holds_unacked_entries_until_claimed(
    redis_endpoint: str,
) -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(redis_endpoint, decode_responses=True)
    stream = "fakeyou:jobs:harness-redis-1"
    try:
        await client.delete(stream)
        entry_id = await client.xadd(
            stream,
            {
                "v": "1",
                "type": "ingestion_job",
                "at": "2026-10-16T09:00:00+00:00",
                "meeting_id": "harness-redis-1",
            },
        )
        await client.xgroup_create(stream, "workers", id="0", mkstream=True)
        delivered = await client.xreadgroup("workers", "worker-a", {stream: ">"}, count=1)

        assert delivered[0][1][0][0] == entry_id
        assert delivered[0][1][0][1]["type"] == "ingestion_job"
        assert set(delivered[0][1][0][1]) == {"v", "type", "at", "meeting_id"}
        pending = await client.xpending(stream, "workers")
        assert pending["pending"] == 1
        stolen = await client.xreadgroup("workers", "worker-b", {stream: ">"}, count=1)
        assert stolen == []
        claimed = await client.xclaim(stream, "workers", "worker-b", 0, [entry_id])
        assert claimed[0][0] == entry_id
        assert await client.xack(stream, "workers", entry_id) == 1
        pending = await client.xpending(stream, "workers")
        assert pending["pending"] == 0
    finally:
        await client.aclose()


@pytest.mark.container
async def test_redis_stream_trim_keeps_only_the_retained_tail(redis_endpoint: str) -> None:
    from redis.asyncio import Redis

    client = Redis.from_url(redis_endpoint, decode_responses=True)
    stream = "fakeyou:jobs:ingestion"
    try:
        await client.delete(stream)
        ids = [
            await client.xadd(stream, {"v": "1", "type": "ingest", "meeting_id": f"m-{index}"})
            for index in range(5)
        ]
        await client.xtrim(stream, maxlen=3, approximate=False)

        assert await client.xlen(stream) == 3
        retained = await client.xrange(stream, "-", "+")
        assert [entry_id for entry_id, _ in retained] == ids[2:]
    finally:
        await client.aclose()
