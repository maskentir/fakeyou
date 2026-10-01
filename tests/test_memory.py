import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.nodes import EpisodeType
from graphiti_core.search.search_filters import ComparisonOperator
from neo4j.exceptions import ServiceUnavailable

import fakeyou.memory as memory_module
from fakeyou.memory import (
    create_memory_tools,
    create_recall_error_middleware,
    ensure_indexes,
    forget_episode,
    forget_meeting,
    meeting_saga,
    open_graphiti_memory,
    remember_owner_note,
    verify_memory_connection,
)
from fakeyou.settings import DEFAULTS, MemorySettings, RecallSettings
from tests.fakes import FakeSaga, make_graphiti


@pytest.mark.parametrize("group_id", ["", "bad:group", "bad group", "x" * 129])
def test_memory_tools_reject_invalid_group_ids(group_id: str) -> None:
    with pytest.raises(ValueError, match="group_id"):
        create_memory_tools(make_graphiti(), group_id=group_id)


async def test_memory_tools_keep_group_id_scoped(
    graphiti,
) -> None:
    memory = graphiti
    remember, recall = create_memory_tools(memory, group_id="team_alpha")

    await remember.ainvoke({"fact": "Alice owns the agenda"})
    await recall.ainvoke({"query": "agenda"})

    assert memory.added[0]["group_id"] == "team_alpha"
    assert memory.searched[0] == {
        "query": "agenda",
        "group_ids": ["team_alpha"],
        "num_results": DEFAULTS.memory.recall.search_limit,
        "search_filter": memory_module.CURRENT_FACTS_FILTER,
    }


def _config() -> MemorySettings:
    return MemorySettings.model_validate(
        {
            "store": {"uri": "bolt://localhost:7687", "user": "neo4j", "password": "p"},
            "llm": {
                "client": "openai",
                "api_key": "k",
                "model": "memory-model",
                "small_model": "memory-small",
            },
            "embedder": {"api_key": "k", "model": "memory-embedding"},
        }
    )


def test_graphiti_memory_uses_explicit_models() -> None:
    memory = memory_module.create_graphiti_memory(_config())

    assert memory.llm_client.config.model == "memory-model"
    assert memory.llm_client.config.small_model == "memory-small"
    assert memory.embedder.config.embedding_model == "memory-embedding"
    assert memory.embedder.config.embedding_dim == 1024


def test_graphiti_memory_embeds_through_a_separate_endpoint() -> None:
    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                **_config().model_dump(),
                "embedder": {
                    **_config().embedder.model_dump(),
                    "base_url": "http://embeddings:11434/v1",
                    "api_key": "local",
                    "dim": 768,
                },
            }
        )
    )

    assert str(memory.embedder.client.base_url) == "http://embeddings:11434/v1/"
    assert memory.embedder.client.api_key == "local"
    assert memory.embedder.config.embedding_dim == 768
    assert memory.llm_client.client.api_key == "k"


def test_graphiti_memory_extracts_through_an_openai_compatible_endpoint() -> None:
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    memory = memory_module.create_graphiti_memory(
        MemorySettings.model_validate(
            {
                **_config().model_dump(),
                "llm": {
                    **_config().llm.model_dump(),
                    "client": "openai_generic",
                    "base_url": "https://api.meta.ai/v1",
                    "api_key": "meta-key",
                },
            }
        )
    )

    assert isinstance(memory.llm_client, OpenAIGenericClient)
    assert memory.llm_client.structured_output_mode == "json_schema"
    assert str(memory.llm_client.client.base_url) == "https://api.meta.ai/v1/"
    assert memory.llm_client.client.api_key == "meta-key"
    assert memory.embedder.client.api_key == "k"


async def test_open_graphiti_memory_is_lightweight(
    graphiti, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory = graphiti
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    async with open_graphiti_memory(
        MemorySettings.model_validate(
            {
                **_config().model_dump(),
                "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
            }
        )
    ) as opened:
        assert opened is memory

    assert memory.events == ["close"]


async def test_ensure_indexes_builds_on_demand(
    graphiti,
) -> None:
    memory = graphiti

    await ensure_indexes(memory)

    assert memory.events == ["build"]


async def test_recall_caps_results() -> None:
    memory = make_graphiti(facts=[f"fact-{index}" for index in range(8)])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    result = await recall.ainvoke({"query": "agenda"})

    assert result.splitlines() == [f"From an unknown source: fact-{index}" for index in range(5)]
    assert memory.searched[0]["num_results"] == DEFAULTS.memory.recall.search_limit


async def test_recall_drops_identical_facts_before_applying_the_limit() -> None:
    memory = make_graphiti(facts=["A", "A", "B", "C", "C", "D", "E", "F"])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    result = await recall.ainvoke({"query": "agenda"})

    facts = [line.removeprefix("From an unknown source: ") for line in result.splitlines()]
    assert facts == list("ABCDE")


def test_current_facts_filter_excludes_invalidated_and_expired_edges() -> None:
    search_filter = memory_module.CURRENT_FACTS_FILTER

    assert search_filter.invalid_at is not None and search_filter.expired_at is not None
    for groups in (search_filter.invalid_at, search_filter.expired_at):
        assert [[item.comparison_operator for item in group] for group in groups] == [
            [ComparisonOperator.is_null]
        ]


async def test_recall_labels_each_fact_with_its_episode_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    episodes = {
        "e1": SimpleNamespace(
            uuid="e1",
            source_description="Google Meet m1 participant transcript",
            valid_at=datetime(2026, 9, 23, 10, tzinfo=UTC),
        ),
        "e2": SimpleNamespace(
            uuid="e2",
            source_description="Google Meet m2 twin speech",
            valid_at=datetime(2026, 10, 1, 0, 30, tzinfo=timezone(timedelta(hours=7))),
        ),
    }
    lookups: list[tuple] = []

    async def get_by_uuids(driver, uuids):
        lookups.append((driver, list(uuids)))
        return [episodes[uuid] for uuid in uuids if uuid in episodes]

    monkeypatch.setattr(memory_module.EpisodicNode, "get_by_uuids", get_by_uuids)
    memory = make_graphiti(
        facts=["Demo moves to Thursday", "Budget is final", "Venue is Hall A"],
        episodes={
            "Demo moves to Thursday": ["e2", "e1"],
            "Budget is final": ["e2"],
            "Venue is Hall A": ["missing"],
        },
    )
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    result = await recall.ainvoke({"query": "demo"})

    assert result.splitlines() == [
        "From Google Meet m1 participant transcript; Google Meet m2 twin speech "
        "on 2026-09-23 10:00 UTC: Demo moves to Thursday",
        "From Google Meet m2 twin speech on 2026-09-30 17:30 UTC: Budget is final",
        "From an unknown source: Venue is Hall A",
    ]
    assert lookups == [(memory.driver, ["e1", "e2", "missing"])]


async def test_recall_empty_returns_sentinel() -> None:
    memory = make_graphiti(facts=[])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "agenda"}) == memory_module.RECALL_EMPTY_RESULT
    assert memory_module.RECALL_EMPTY_RESULT.strip() != ""


async def test_verify_memory_connection_fails_fast_without_transaction_retry() -> None:
    driver = await asyncio.to_thread(Neo4jDriver, "bolt://127.0.0.1:9", "neo4j", "password")
    try:
        async with asyncio.timeout(5):
            with pytest.raises(ServiceUnavailable):
                await verify_memory_connection(SimpleNamespace(driver=driver))
    finally:
        await driver.close()


async def test_verify_memory_connection_requires_neo4j_driver() -> None:
    with pytest.raises(TypeError, match="Neo4j"):
        await verify_memory_connection(SimpleNamespace(driver=object()))


def test_recall_error_middleware_returns_unknown_and_records_fault(
    journal,
) -> None:
    from fakeyou.faults import FaultReporter

    lines = journal.lines
    faults = FaultReporter(journal=journal)
    middleware = create_recall_error_middleware(faults)
    request = SimpleNamespace(tool_call={"name": "recall_meeting_context", "id": "c1"})

    assert middleware.on_error is not None
    content = middleware.on_error(RuntimeError("secret detail"), request)

    assert content == memory_module.RECALL_UNAVAILABLE_RESULT
    assert faults.total() == 1
    assert "secret detail" not in lines[0]


async def test_forget_meeting_removes_every_saga_episode_and_the_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saga = meeting_saga("meeting-1")
    memory = make_graphiti(
        saga_episodes={saga: [f"e{index}" for index in range(5)], "meeting:other": ["x"]}
    )
    deleted: list[str] = []
    saga_groups: list[list[str]] = []

    async def get_by_group_ids(driver, group_ids):
        saga_groups.append(group_ids)
        return [FakeSaga(saga, deleted), FakeSaga("meeting:other", deleted)]

    monkeypatch.setattr(memory_module, "FORGET_BATCH_SIZE", 2)
    monkeypatch.setattr(memory_module.SagaNode, "get_by_group_ids", get_by_group_ids)

    removed = await forget_meeting(memory, group_id="team_alpha", meeting_id="meeting-1")

    assert removed == 5
    assert memory.removed == [f"e{index}" for index in range(5)]
    assert {call["group_ids"][0] for call in memory.retrieved} == {"team_alpha"}
    assert all(call["reference_time"] > datetime.now(UTC) for call in memory.retrieved)
    assert deleted == [saga]
    assert saga_groups == [["team_alpha"]]


async def test_forget_meeting_stops_when_an_episode_survives_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saga = meeting_saga("meeting-1")
    memory = make_graphiti(saga_episodes={saga: ["stuck"]})

    async def remove_without_effect(episode_uuid: str) -> None:
        return None

    memory.remove_episode = remove_without_effect

    with pytest.raises(RuntimeError, match="stuck"):
        await forget_meeting(memory, group_id="team_alpha", meeting_id="meeting-1")


@pytest.mark.parametrize("group_id,allowed", [("team_alpha", True), ("team_beta", False)])
async def test_forget_episode_only_removes_episodes_in_the_persona_group(
    monkeypatch: pytest.MonkeyPatch, group_id: str, allowed: bool
) -> None:
    memory = make_graphiti(saga_episodes={})

    async def get_by_uuid(driver, uuid):
        return SimpleNamespace(uuid=uuid, group_id=group_id)

    monkeypatch.setattr(memory_module.EpisodicNode, "get_by_uuid", get_by_uuid)

    if allowed:
        await forget_episode(memory, group_id="team_alpha", episode_uuid="e1")
        assert memory.removed == ["e1"]
    else:
        with pytest.raises(ValueError, match="not in this persona"):
            await forget_episode(memory, group_id="team_alpha", episode_uuid="e1")
        assert memory.removed == []


async def test_remember_owner_note_stores_text_episode_in_group() -> None:
    added: list[dict] = []

    memory = make_graphiti(episode_prefix="note-")
    memory.added = added

    result = await remember_owner_note(
        memory, group_id="team_alpha", text="  The Orchid demo moves to Friday.  "
    )

    assert result.episode.uuid == "note-1"
    [note] = added
    assert note["episode_body"] == "The Orchid demo moves to Friday."
    assert note["source"] == EpisodeType.text
    assert note["source_description"] == memory_module.OWNER_NOTE_SOURCE
    assert note["group_id"] == "team_alpha"
    assert note["name"].startswith("owner-note:")
    assert note["reference_time"].tzinfo is not None


async def test_recall_times_out_with_its_configured_budget() -> None:
    _remember, recall = create_memory_tools(
        make_graphiti(search_gate=asyncio.Event()),
        group_id="team_alpha",
        recall=RecallSettings(timeout_seconds=0.01),
    )

    with pytest.raises(TimeoutError):
        await recall.ainvoke({"query": "agenda"})


def test_default_recall_budget_fits_inside_the_reply_deadline() -> None:
    assert DEFAULTS.memory.recall.timeout_seconds < DEFAULTS.conversation.response_timeout_seconds


async def test_reembed_group_pages_through_entities_and_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved: list[tuple[str, list[float]]] = []

    class Item(SimpleNamespace):
        async def save(self, driver):
            saved.append((self.uuid, getattr(self, "name_embedding", None) or self.fact_embedding))

    nodes = [Item(uuid=f"n{i}", name=f"entity {i}", name_embedding=None) for i in (3, 2, 1)]
    edges = [Item(uuid=f"e{i}", fact=f"fact {i}", fact_embedding=None) for i in (2, 1)]
    pages: list[tuple[str, str | None]] = []

    def pager(kind, items, empty_error=None):
        async def get_by_group_ids(driver, group_ids, limit=None, uuid_cursor=None):
            pages.append((kind, uuid_cursor))
            assert group_ids == ["twin_group"]
            remaining = [item for item in items if uuid_cursor is None or item.uuid < uuid_cursor]
            if not remaining and empty_error is not None:
                raise empty_error(group_ids)
            return remaining[:2]

        return get_by_group_ids

    monkeypatch.setattr(memory_module, "REEMBED_BATCH_SIZE", 2)
    monkeypatch.setattr(memory_module.EntityNode, "get_by_group_ids", pager("node", nodes))
    monkeypatch.setattr(
        memory_module.EntityEdge,
        "get_by_group_ids",
        pager("edge", edges, memory_module.GroupsEdgesNotFoundError),
    )

    class Embedder:
        async def create_batch(self, texts):
            return [[float(len(text)), 1.0] for text in texts]

    memory = SimpleNamespace(driver="driver", embedder=Embedder())

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


@pytest.mark.parametrize("provider", ["openai", "openai_generic", "anthropic"])
@pytest.mark.parametrize("fail_body", [False, True])
async def test_memory_context_closes_owned_transports(provider, fail_body, monkeypatch):
    config = MemorySettings.model_validate(
        {
            **_config().model_dump(),
            "llm": {**_config().llm.model_dump(), "client": provider},
            "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
        }
    )
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    clients = []
    closed = []
    for sdk in (AsyncOpenAI, AsyncAnthropic):
        original = sdk.close

        async def close(client, original=original):
            closed.append(client)
            await original(client)

        monkeypatch.setattr(sdk, "close", close)
    try:
        async with open_graphiti_memory(config) as memory:
            clients = [memory.llm_client.client, memory.embedder.client]
            if provider != "anthropic":
                clients.append(memory.cross_encoder.client)
            else:
                assert memory.cross_encoder.client is memory.llm_client
            if fail_body:
                raise RuntimeError("setup failed")
    except RuntimeError as error:
        assert fail_body and str(error) == "setup failed"
    try:
        assert all(client.is_closed() for client in clients)
        assert [closed.count(client) for client in clients] == [1] * len(clients)
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize(
    "component,provider",
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
    component, provider, monkeypatch
):
    from openai import AsyncOpenAI

    clients = []
    original_anthropic = memory_module.GraphitiAnthropicSDK

    def anthropic(*args, **kwargs):
        client = original_anthropic(*args, **kwargs)
        clients.append(client)
        return client

    def openai(*args, **kwargs):
        client = AsyncOpenAI(*args, **kwargs)
        clients.append(client)
        return client

    def fail(*args, **kwargs):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(memory_module, "GraphitiAnthropicSDK", anthropic)
    monkeypatch.setattr(memory_module, "AsyncOpenAI", openai, raising=False)
    drivers = []
    if component == "Graphiti":
        original_driver = memory_module.Neo4jDriver

        def driver(*args, **kwargs):
            created = original_driver(*args, **kwargs)
            drivers.append(created)
            return created

        monkeypatch.setattr(memory_module, "Neo4jDriver", driver)
    monkeypatch.setattr(memory_module, component, fail)
    config = MemorySettings.model_validate(
        {
            **_config().model_dump(),
            "llm": {**_config().llm.model_dump(), "client": provider},
            "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
        }
    )
    try:
        with pytest.raises(RuntimeError, match="construction failed"):
            async with open_graphiti_memory(config):
                pytest.fail("construction must fail before yielding")
        assert clients
        assert all(client.is_closed() for client in clients)
        assert all(driver.client._closed for driver in drivers)
    finally:
        for client in clients:
            await client.close()


async def test_memory_context_waits_for_construction_cleanup_when_cancelled(monkeypatch):
    import threading

    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    constructed = []
    original = memory_module.create_graphiti_memory

    def construct(config, **kwargs):
        memory = original(config, **kwargs)
        constructed.append(memory)
        loop.call_soon_threadsafe(started.set)
        release.wait(2)
        return memory

    monkeypatch.setattr(memory_module, "create_graphiti_memory", construct)
    config = MemorySettings.model_validate(
        {
            **_config().model_dump(),
            "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
        }
    )

    async def open_memory():
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
        memory = constructed[0]
        assert memory.driver.client._closed
        assert memory.llm_client.client.is_closed()
        assert memory.embedder.client.is_closed()
        assert memory.cross_encoder.client.is_closed()
    finally:
        release.set()
        for memory in constructed:
            await memory.close()
            await memory.llm_client.client.close()
            await memory.embedder.client.close()
            await memory.cross_encoder.client.close()


@pytest.mark.parametrize("fail_construction", [False, True])
async def test_memory_context_finishes_late_construction_after_repeated_cancellation(
    monkeypatch, fail_construction
):
    import threading

    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    constructed = []
    clients = []
    original_factory = memory_module.create_graphiti_memory
    original_embedder = memory_module.OpenAIEmbedder
    original_sdk = memory_module.AsyncOpenAI

    def sdk(*args, **kwargs):
        client = original_sdk(*args, **kwargs)
        clients.append(client)
        return client

    def embedder(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        if not release.wait(2):
            raise RuntimeError("construction gate timed out")
        if fail_construction:
            raise RuntimeError("late construction failed")
        return original_embedder(*args, **kwargs)

    def construct(config, **kwargs):
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
            **_config().model_dump(),
            "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
        }
    )

    async def open_memory():
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
            assert constructed[0].driver.client._closed
    finally:
        release.set()
        await asyncio.to_thread(finished.wait, 2)
        await asyncio.gather(opening, return_exceptions=True)
        for memory in constructed:
            await memory.close()
        for client in clients:
            await client.close()


async def test_memory_context_finishes_disposal_after_repeated_cancellation(monkeypatch):
    from openai import AsyncOpenAI

    started = asyncio.Event()
    release = asyncio.Event()
    clients = []
    original_close = AsyncOpenAI.close

    async def close(client):
        if not started.is_set():
            started.set()
            await release.wait()
        await original_close(client)

    monkeypatch.setattr(AsyncOpenAI, "close", close)
    config = MemorySettings.model_validate(
        {
            **_config().model_dump(),
            "embedder": {**_config().embedder.model_dump(), "dim": memory_module.EMBEDDING_DIM},
        }
    )

    async def open_memory():
        async with open_graphiti_memory(config) as memory:
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
