import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.embedder.openai import OpenAIEmbedder
from graphiti_core.errors import GroupsEdgesNotFoundError
from graphiti_core.nodes import EntityNode
from neo4j.exceptions import ServiceUnavailable

import fakeyou.memory.memory as memory_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS, IngestionSettings, MemorySettings
from fakeyou.memory import owner_notes
from fakeyou.memory.memory import (
    create_memory_tools,
    ensure_indexes,
    open_graphiti_memory,
    verify_memory_connection,
)
from tests._memory_fakes import make_graphiti
from tests._memory_support import memory_settings


@pytest.mark.parametrize("group_id", ["", "bad:group", "bad group", "x" * 129])
def test_memory_tools_reject_invalid_group_ids(group_id: str) -> None:
    with pytest.raises(FakeYouError, match="group_id") as caught:
        create_memory_tools(make_graphiti(), group_id=group_id)
    assert caught.value.code is ErrorCode.NOTE_GROUP_ID_INVALID


async def test_memory_tools_keep_group_id_scoped(
    graphiti: MagicMock,
) -> None:
    memory = graphiti
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    await recall.ainvoke({"query": "agenda"})

    assert memory.added == []
    assert memory.searched[0] == {
        "query": "agenda",
        "group_ids": ["team_alpha"],
        "num_results": DEFAULTS.memory.recall.search_limit,
        "search_filter": memory_module.CURRENT_FACTS_FILTER,
    }


async def test_query_sensitive_fake_memory_matches_facts_by_query_tokens() -> None:
    memory = make_graphiti(
        facts=["Demo Orchid moved to Friday", "Rani leads QA"],
        query_sensitive=True,
    )

    orchid = await memory.search("When is the Orchid demo?")
    assert [edge.fact for edge in orchid] == ["Demo Orchid moved to Friday"]
    qa = await memory.search("Who leads QA?")
    assert [edge.fact for edge in qa] == ["Rani leads QA"]
    assert await memory.search("What is the budget status?") == []


async def test_match_all_fake_memory_returns_case_facts_for_any_query() -> None:
    memory = make_graphiti(
        facts=["Bridge repaint starts June."],
        query_sensitive=True,
        match_all=True,
    )

    edges = await memory.search("What is the budget status?")

    assert [edge.fact for edge in edges] == ["Bridge repaint starts June."]


def test_graphiti_memory_uses_explicit_models() -> None:
    memory = memory_module.create_graphiti_memory(memory_settings())

    assert isinstance(memory.embedder, OpenAIEmbedder)
    assert memory.llm_client.config.model == "memory-model"
    assert memory.llm_client.config.small_model == "memory-small"
    assert memory.embedder.config.embedding_model == "memory-embedding"
    assert memory.embedder.config.embedding_dim == 1024


def test_graphiti_memory_embeds_through_a_separate_endpoint() -> None:
    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                **memory_settings().model_dump(),
                "embedder": {
                    **memory_settings().embedder.model_dump(),
                    "base_url": "http://embeddings:11434/v1",
                    "api_key": "local",
                    "dim": 768,
                },
            }
        )
    )

    from graphiti_core.llm_client.openai_client import OpenAIClient

    assert isinstance(memory.embedder, OpenAIEmbedder)
    assert isinstance(memory.llm_client, OpenAIClient)
    assert str(memory.embedder.client.base_url) == "http://embeddings:11434/v1/"
    assert memory.embedder.client.api_key == "local"
    assert memory.embedder.config.embedding_dim == 768
    assert memory.llm_client.client.api_key == "k"


def test_graphiti_memory_extracts_through_an_openai_compatible_endpoint() -> None:
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                **memory_settings().model_dump(),
                "llm": {
                    **memory_settings().llm.model_dump(),
                    "client": "openai_generic",
                    "base_url": "https://api.meta.ai/v1",
                    "api_key": "meta-key",
                },
            }
        )
    )

    assert isinstance(memory.llm_client, OpenAIGenericClient)
    assert isinstance(memory.embedder, OpenAIEmbedder)
    assert memory.llm_client.structured_output_mode == "json_schema"
    assert str(memory.llm_client.client.base_url) == "https://api.meta.ai/v1/"
    assert memory.llm_client.client.api_key == "meta-key"
    assert memory.embedder.client.api_key == "k"


async def test_open_graphiti_memory_is_lightweight(
    graphiti: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = graphiti
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    async with open_graphiti_memory(
        MemorySettings.model_validate(
            {
                **memory_settings().model_dump(),
                "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
            }
        )
    ) as opened:
        assert opened is memory

    assert memory.events == ["close"]


async def test_ensure_indexes_builds_on_demand(
    graphiti: MagicMock,
) -> None:
    memory = graphiti

    await ensure_indexes(memory)

    assert memory.events == ["build"]


async def test_verify_memory_connection_fails_fast_without_transaction_retry() -> None:
    driver = await asyncio.to_thread(Neo4jDriver, "bolt://127.0.0.1:9", "neo4j", "password")
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ServiceUnavailable):
                await verify_memory_connection(cast(Graphiti, SimpleNamespace(driver=driver)))
    finally:
        await driver.close()


async def test_verify_memory_connection_requires_neo4j_driver() -> None:
    with pytest.raises(TypeError, match="Neo4j"):
        await verify_memory_connection(cast(Graphiti, SimpleNamespace(driver=object())))


async def test_ensure_indexes_initializes_note_controls(
    graphiti: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = graphiti
    calls: list[Any] = []

    async def guarded_indexes(note_memory: Any) -> None:
        calls.append(note_memory)

    assert memory_module.ensure_note_indexes is owner_notes.ensure_note_indexes
    monkeypatch.setattr(memory_module, "ensure_note_indexes", guarded_indexes)

    await ensure_indexes(memory)

    assert memory.events == ["build"]
    assert calls == [memory]


async def test_reembed_group_pages_through_entities_and_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[tuple[str, list[float]]] = []

    class Item(SimpleNamespace):
        async def save(self, driver: Any) -> None:
            del driver
            saved.append((self.uuid, getattr(self, "name_embedding", None) or self.fact_embedding))

    nodes = [Item(uuid=f"n{i}", name=f"entity {i}", name_embedding=None) for i in (3, 2, 1)]
    edges = [Item(uuid=f"e{i}", fact=f"fact {i}", fact_embedding=None) for i in (2, 1)]
    pages: list[tuple[str, str | None]] = []

    def pager(
        kind: str, items: list[Any], empty_error: Callable[..., BaseException] | None = None
    ) -> Callable[..., Any]:
        async def get_by_group_ids(
            driver: Any,
            group_ids: list[str],
            limit: int | None = None,
            uuid_cursor: str | None = None,
        ) -> list[Any]:
            del driver
            del limit
            pages.append((kind, uuid_cursor))
            assert group_ids == ["twin_group"]
            remaining = [item for item in items if uuid_cursor is None or item.uuid < uuid_cursor]
            if not remaining and empty_error is not None:
                raise empty_error(group_ids)
            return remaining[:2]

        return get_by_group_ids

    monkeypatch.setattr(memory_module, "REEMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(EntityNode, "get_by_group_ids", pager("node", nodes))
    monkeypatch.setattr(
        EntityEdge,
        "get_by_group_ids",
        pager("edge", edges, GroupsEdgesNotFoundError),
    )

    class Embedder:
        async def create_batch(self, texts: list[str]) -> list[list[float]]:
            return [[float(len(text)), 1.0] for text in texts]

    memory = cast(Graphiti, SimpleNamespace(driver="driver", embedder=Embedder()))

    result = await memory_module.reembed_group(memory, group_id="twin_group")

    assert (result.entities, result.facts) == (3, 2)
    assert pages == [
        ("node", None),
        ("node", "n2"),
        ("node", "n1"),
        ("edge", None),
        ("edge", "e1"),
    ]
    assert saved == [
        ("n3", [8.0, 1.0]),
        ("n2", [8.0, 1.0]),
        ("n1", [8.0, 1.0]),
        ("e2", [6.0, 1.0]),
        ("e1", [6.0, 1.0]),
    ]


def test_create_graphiti_memory_wires_extraction_concurrency() -> None:
    config = memory_settings().model_copy(
        update={"ingestion": IngestionSettings(extraction_concurrency=7)}
    )

    memory = memory_module.create_graphiti_memory(config)

    assert memory.max_coroutines == 7


def test_create_graphiti_memory_defaults_extraction_concurrency_to_the_frozen_twenty() -> None:
    assert memory_module.create_graphiti_memory(memory_settings()).max_coroutines == 20
