import asyncio
from typing import Any

import pytest
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.embedder.openai import OpenAIEmbedder
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.openai_client import OpenAIClient

import fakeyou.memory.memory as memory_module
from fakeyou.config.settings import MemorySettings
from tests._memory_support import memory_settings


def test_graphiti_sdks_use_configured_timeouts_and_retries() -> None:
    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                "store": {"uri": "bolt://localhost:7687", "user": "neo4j", "password": "p"},
                "llm": {
                    "client": "openai",
                    "api_key": "k",
                    "timeout_seconds": 11.0,
                    "max_retries": 5,
                },
                "embedder": {"api_key": "e", "timeout_seconds": 12.0, "max_retries": 6},
            }
        )
    )

    llm_client = memory.llm_client
    embedder = memory.embedder
    cross_encoder = memory.cross_encoder
    assert isinstance(llm_client, OpenAIClient)
    assert isinstance(embedder, OpenAIEmbedder)
    assert isinstance(cross_encoder, OpenAIRerankerClient)
    assert (llm_client.client.timeout, llm_client.client.max_retries) == (11.0, 5)
    assert (embedder.client.timeout, embedder.client.max_retries) == (12.0, 6)
    assert (cross_encoder.client.timeout, cross_encoder.client.max_retries) == (11.0, 5)


def test_graphiti_anthropic_sdk_uses_configured_timeouts_and_retries() -> None:
    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                "store": {"uri": "bolt://localhost:7687", "user": "neo4j", "password": "p"},
                "llm": {
                    "client": "anthropic",
                    "api_key": "k",
                    "timeout_seconds": 13.0,
                    "max_retries": 4,
                },
                "embedder": {"api_key": "e"},
            }
        )
    )

    llm_client = memory.llm_client
    assert isinstance(llm_client, AnthropicClient)
    assert (llm_client.client.timeout, llm_client.client.max_retries) == (13.0, 4)


async def test_graphiti_memory_close_aborts_a_hanging_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_module

    from graphiti_core import Graphiti

    async def hanging_close(self: Any) -> None:
        del self
        await asyncio.sleep(5.0)

    monkeypatch.setattr(Graphiti, "close", hanging_close)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
            "close_callback_seconds": 0.05,
        }
    )
    started = time_module.monotonic()
    with pytest.raises(TimeoutError):
        async with memory_module.open_graphiti_memory(config):
            pass
    elapsed = time_module.monotonic() - started

    assert elapsed < 3.0


async def test_graphiti_memory_close_keeps_body_error_over_teardown_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphiti_core import Graphiti

    async def hanging_close(self: Any) -> None:
        del self
        await asyncio.sleep(5.0)

    monkeypatch.setattr(Graphiti, "close", hanging_close)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
            "close_callback_seconds": 0.05,
        }
    )
    with pytest.raises(ValueError, match="body failed"):
        async with memory_module.open_graphiti_memory(config):
            raise ValueError("body failed")


async def test_a_failing_body_keeps_its_error_when_close_overruns_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from openai import AsyncOpenAI

    async def hanging_close(self: Any) -> None:
        del self
        await asyncio.sleep(5.0)

    monkeypatch.setattr(Graphiti, "close", hanging_close)
    monkeypatch.setattr(AsyncOpenAI, "close", hanging_close)
    monkeypatch.setattr(Neo4jDriver, "close", hanging_close)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
            "close_callback_seconds": 0.05,
            "close_hard_seconds": 0.1,
        }
    )
    with pytest.raises(ValueError, match="body failed"):
        async with memory_module.open_graphiti_memory(config):
            raise ValueError("body failed")


async def test_graphiti_memory_close_raises_past_the_hard_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time as time_module

    from graphiti_core import Graphiti
    from graphiti_core.driver.neo4j_driver import Neo4jDriver
    from openai import AsyncOpenAI

    async def hanging_close(self: Any) -> None:
        del self
        await asyncio.sleep(5.0)

    monkeypatch.setattr(Graphiti, "close", hanging_close)
    monkeypatch.setattr(AsyncOpenAI, "close", hanging_close)
    monkeypatch.setattr(Neo4jDriver, "close", hanging_close)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
            "close_callback_seconds": 0.05,
            "close_hard_seconds": 0.1,
        }
    )
    started = time_module.monotonic()
    with pytest.raises(TimeoutError, match="hard deadline"):
        async with memory_module.open_graphiti_memory(config):
            pass
    elapsed = time_module.monotonic() - started

    assert elapsed < 3.0
