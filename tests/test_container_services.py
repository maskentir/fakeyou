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
