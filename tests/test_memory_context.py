import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.embedder.openai import OpenAIEmbedder
from neo4j.exceptions import DriverError
from openai import AsyncOpenAI

import fakeyou.memory.memory as memory_module
from fakeyou.config.settings import MemorySettings
from fakeyou.memory.memory import AnthropicReranker, GraphitiAnthropicSDK, open_graphiti_memory
from tests._memory_support import memory_settings


def _assert_driver_closed(driver: Neo4jDriver) -> None:
    with pytest.raises(DriverError, match="Driver closed"):
        driver.client.session()


@pytest.mark.parametrize("provider", ["openai", "openai_generic", "anthropic"])
@pytest.mark.parametrize("fail_body", [False, True])
async def test_memory_context_closes_owned_transports(
    provider: str, *, fail_body: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "llm": {**memory_settings().llm.model_dump(), "client": provider},
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
        }
    )
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    clients = []
    closed = []
    for sdk in (AsyncOpenAI, AsyncAnthropic):
        original = sdk.close

        async def close(client: Any, original: Callable[..., Any] = original) -> Any:
            closed.append(client)
            await original(client)

        monkeypatch.setattr(sdk, "close", close)
    error: RuntimeError | None = None
    try:
        async with open_graphiti_memory(config) as memory:
            from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
            from graphiti_core.llm_client.anthropic_client import AnthropicClient
            from graphiti_core.llm_client.openai_client import OpenAIClient
            from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

            if provider == "anthropic":
                assert isinstance(memory.llm_client, AnthropicClient)
            elif provider == "openai":
                assert isinstance(memory.llm_client, OpenAIClient)
            else:
                assert isinstance(memory.llm_client, OpenAIGenericClient)
            assert isinstance(memory.embedder, OpenAIEmbedder)
            clients = [memory.llm_client.client, memory.embedder.client]
            if provider != "anthropic":
                assert isinstance(memory.cross_encoder, OpenAIRerankerClient)
                clients.append(memory.cross_encoder.client)
            else:
                assert isinstance(memory.cross_encoder, AnthropicReranker)
                assert memory.cross_encoder.client is memory.llm_client
            if fail_body:
                raise RuntimeError("setup failed")
    except RuntimeError as caught:
        error = caught
    assert (error is not None) == fail_body
    if error is not None:
        assert str(error) == "setup failed"
    try:
        assert all(client.is_closed() for client in clients)
        assert [closed.count(client) for client in clients] == [1] * len(clients)
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize(
    ("component", "provider"),
    [
        ("AnthropicClient", "anthropic"),
        ("OpenAIClient", "openai"),
        ("OpenAIEmbedder", "anthropic"),
        ("OpenAIRerankerClient", "openai"),
        ("Neo4jDriver", "anthropic"),
        ("Graphiti", "anthropic"),
    ],
)
async def test_memory_context_closes_transports_on_partial_construction(
    component: str, provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients: list[Any] = []
    original_anthropic = GraphitiAnthropicSDK

    def anthropic(*args: Any, **kwargs: Any) -> Any:
        client = original_anthropic(*args, **kwargs)
        clients.append(client)
        return client

    def openai(*args: Any, **kwargs: Any) -> Any:
        client = AsyncOpenAI(*args, **kwargs)
        clients.append(client)
        return client

    def fail(*args: Any, **kwargs: Any) -> Any:
        del args
        del kwargs
        raise RuntimeError("construction failed")

    monkeypatch.setattr(memory_module, "GraphitiAnthropicSDK", anthropic)
    monkeypatch.setattr(memory_module, "AsyncOpenAI", openai, raising=False)
    drivers: list[Any] = []
    if component == "Graphiti":
        original_driver = Neo4jDriver

        def driver(*args: Any, **kwargs: Any) -> Any:
            created = original_driver(*args, **kwargs)
            drivers.append(created)
            return created

        monkeypatch.setattr(memory_module, "Neo4jDriver", driver)
    monkeypatch.setattr(memory_module, component, fail)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "llm": {**memory_settings().llm.model_dump(), "client": provider},
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
        }
    )
    try:
        with pytest.raises(RuntimeError, match="construction failed"):
            async with open_graphiti_memory(config):
                pytest.fail("construction must fail before yielding")
        assert clients
        assert all(client.is_closed() for client in clients)
        for created in drivers:
            _assert_driver_closed(created)
    finally:
        for client in clients:
            await client.close()


async def test_memory_context_waits_for_construction_cleanup_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    constructed = []
    original = memory_module.create_graphiti_memory

    def construct(config: MemorySettings, **kwargs: Any) -> Graphiti:
        memory = original(config, **kwargs)
        constructed.append(memory)
        loop.call_soon_threadsafe(started.set)
        release.wait(2)
        return memory

    monkeypatch.setattr(memory_module, "create_graphiti_memory", construct)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
        }
    )

    async def open_memory() -> None:
        async with open_graphiti_memory(config):
            pytest.fail("cancelled construction must not yield")

    opening = asyncio.create_task(open_memory())
    try:
        await started.wait()
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.llm_client.openai_client import OpenAIClient

        memory = constructed[0]
        assert isinstance(memory.driver, Neo4jDriver)
        assert isinstance(memory.llm_client, OpenAIClient)
        assert isinstance(memory.embedder, OpenAIEmbedder)
        assert isinstance(memory.cross_encoder, OpenAIRerankerClient)
        _assert_driver_closed(memory.driver)
        assert memory.llm_client.client.is_closed()
        assert memory.embedder.client.is_closed()
        assert memory.cross_encoder.client.is_closed()
    finally:
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.llm_client.openai_client import OpenAIClient

        release.set()
        for memory in constructed:
            closer: Callable[[], Coroutine[Any, Any, None]] = memory.close
            await closer()
            assert isinstance(memory.llm_client, OpenAIClient)
            assert isinstance(memory.embedder, OpenAIEmbedder)
            assert isinstance(memory.cross_encoder, OpenAIRerankerClient)
            await memory.llm_client.client.close()
            await memory.embedder.client.close()
            await memory.cross_encoder.client.close()


async def _settle_late_construction(
    release: threading.Event,
    finished: threading.Event,
    opening: asyncio.Task[None],
    constructed: list[Graphiti],
    clients: list[Any],
) -> None:
    release.set()
    await asyncio.to_thread(finished.wait, 2)
    await asyncio.gather(opening, return_exceptions=True)
    for memory in constructed:
        closer: Callable[[], Coroutine[Any, Any, None]] = memory.close
        await closer()
    for client in clients:
        await client.close()


@pytest.mark.parametrize("fail_construction", [False, True])
async def test_memory_context_finishes_late_construction_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch, *, fail_construction: bool
) -> None:
    import threading

    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    constructed: list[Graphiti] = []
    clients: list[Any] = []
    original_factory = memory_module.create_graphiti_memory
    original_embedder = OpenAIEmbedder
    original_sdk = AsyncOpenAI

    def sdk(*args: Any, **kwargs: Any) -> Any:
        client = original_sdk(*args, **kwargs)
        clients.append(client)
        return client

    def embedder(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(2):
            raise RuntimeError("construction gate timed out")
        if fail_construction:
            raise RuntimeError("late construction failed")
        return original_embedder(*args, **kwargs)

    def construct(config: MemorySettings, **kwargs: Any) -> Graphiti:
        try:
            memory = original_factory(config, **kwargs)
            constructed.append(memory)
            return memory
        finally:
            finished.set()

    monkeypatch.setattr(memory_module, "AsyncOpenAI", sdk)
    monkeypatch.setattr(memory_module, "OpenAIEmbedder", embedder)
    monkeypatch.setattr(memory_module, "create_graphiti_memory", construct)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
        }
    )

    async def open_memory() -> None:
        async with open_graphiti_memory(config):
            pytest.fail("cancelled construction must not yield")

    opening = asyncio.create_task(open_memory())
    try:
        await started.wait()
        for _ in range(3):
            opening.cancel()
            await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert all(client.is_closed() for client in clients)
        if not fail_construction:
            assert len(clients) == 3
            assert isinstance(constructed[0].driver, Neo4jDriver)
            _assert_driver_closed(constructed[0].driver)
    finally:
        await _settle_late_construction(release, finished, opening, constructed, clients)


async def test_memory_context_finishes_disposal_after_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import AsyncOpenAI

    started = asyncio.Event()
    release = asyncio.Event()
    clients = []
    original_close = AsyncOpenAI.close

    async def close(client: Any) -> None:
        if not started.is_set():
            started.set()
            await release.wait()
        await original_close(client)

    monkeypatch.setattr(AsyncOpenAI, "close", close)
    config = MemorySettings.model_validate(
        {
            **memory_settings().model_dump(),
            "embedder": {**memory_settings().embedder.model_dump(), "dim": EMBEDDING_DIM},
        }
    )

    async def open_memory() -> None:
        from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
        from graphiti_core.llm_client.openai_client import OpenAIClient

        async with open_graphiti_memory(config) as memory:
            assert isinstance(memory.llm_client, OpenAIClient)
            assert isinstance(memory.embedder, OpenAIEmbedder)
            assert isinstance(memory.cross_encoder, OpenAIRerankerClient)
            clients.extend(
                [memory.llm_client.client, memory.embedder.client, memory.cross_encoder.client]
            )

    opening = asyncio.create_task(open_memory())
    try:
        await started.wait()
        for _ in range(3):
            opening.cancel()
            await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert all(client.is_closed() for client in clients)
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        for client in clients:
            await original_close(client)
