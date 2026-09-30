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
    Neo4jMemoryConfig,
    create_memory_tools,
    ensure_indexes,
    forget_episode,
    forget_meeting,
    meeting_saga,
    open_graphiti_memory,
    recall_error_middleware,
    remember_owner_note,
    verify_memory_connection,
)


class FakeMemory:
    def __init__(self) -> None:
        self.added: list[dict[str, object]] = []
        self.searched: list[dict[str, object]] = []

    async def add_episode(self, **kwargs):
        self.added.append(kwargs)

    async def search(self, query, **kwargs):
        self.searched.append({"query": query, **kwargs})
        return []


@pytest.mark.parametrize("group_id", ["", "bad:group", "bad group", "x" * 129])
def test_memory_tools_reject_invalid_group_ids(group_id: str) -> None:
    with pytest.raises(ValueError, match="group_id"):
        create_memory_tools(FakeMemory(), group_id=group_id)


async def test_memory_tools_keep_group_id_scoped() -> None:
    memory = FakeMemory()
    remember, recall = create_memory_tools(memory, group_id="team_alpha")

    await remember.ainvoke({"fact": "Alice owns the agenda"})
    await recall.ainvoke({"query": "agenda"})

    assert memory.added[0]["group_id"] == "team_alpha"
    assert memory.searched[0] == {
        "query": "agenda",
        "group_ids": ["team_alpha"],
        "num_results": memory_module.RECALL_SEARCH_LIMIT,
        "search_filter": memory_module.CURRENT_FACTS_FILTER,
    }


class FakeGraphiti:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def build_indices_and_constraints(self) -> None:
        self.events.append("build")

    async def close(self) -> None:
        self.events.append("close")


def _config() -> Neo4jMemoryConfig:
    return Neo4jMemoryConfig(
        uri="bolt://localhost:7687",
        user="neo4j",
        password="p",
        api_key="k",
        llm_model="memory-model",
        small_model="memory-small",
        embedding_model="memory-embedding",
    )


def test_graphiti_memory_uses_explicit_models() -> None:
    memory = memory_module.create_graphiti_memory(_config())

    assert memory.llm_client.config.model == "memory-model"
    assert memory.llm_client.config.small_model == "memory-small"
    assert memory.embedder.config.embedding_model == "memory-embedding"


async def test_open_graphiti_memory_is_lightweight(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = FakeGraphiti()
    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config: memory)

    async with open_graphiti_memory(_config()) as opened:
        assert opened is memory

    assert memory.events == ["close"]


async def test_ensure_indexes_builds_on_demand() -> None:
    memory = FakeGraphiti()

    await ensure_indexes(memory)

    assert memory.events == ["build"]


class FakeSearchMemory(FakeMemory):
    def __init__(self, facts: list[str], episodes: dict[str, list[str]] | None = None) -> None:
        super().__init__()
        self.facts = facts
        self.fact_episodes = episodes or {}
        self.driver = SimpleNamespace(name="driver")

    async def search(self, query, **kwargs):
        self.searched.append({"query": query, **kwargs})
        return [
            SimpleNamespace(fact=fact, episodes=self.fact_episodes.get(fact, []))
            for fact in self.facts
        ]


async def test_recall_caps_results() -> None:
    memory = FakeSearchMemory([f"fact-{index}" for index in range(8)])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    result = await recall.ainvoke({"query": "agenda"})

    assert result.splitlines() == [f"From an unknown source: fact-{index}" for index in range(5)]
    assert memory.searched[0]["num_results"] == memory_module.RECALL_SEARCH_LIMIT


async def test_recall_drops_identical_facts_before_applying_the_limit() -> None:
    memory = FakeSearchMemory(["A", "A", "B", "C", "C", "D", "E", "F"])
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
    memory = FakeSearchMemory(
        ["Demo moves to Thursday", "Budget is final", "Venue is Hall A"],
        {
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
    memory = FakeSearchMemory([])
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


def test_recall_error_middleware_returns_unknown_and_records_fault() -> None:
    from fakeyou.faults import FaultReporter

    lines: list[str] = []
    faults = FaultReporter(meeting_id="m", log=lines.append)
    middleware = recall_error_middleware(faults)
    request = SimpleNamespace(tool_call={"name": "recall_meeting_context", "id": "c1"})

    assert middleware.on_error is not None
    content = middleware.on_error(RuntimeError("secret detail"), request)

    assert content == memory_module.RECALL_UNAVAILABLE_RESULT
    assert faults.total() == 1
    assert "secret detail" not in lines[0]


class FakeForgetMemory:
    def __init__(self, episodes: dict[str, list[str]]) -> None:
        self.saga_episodes = episodes
        self.removed: list[str] = []
        self.retrieved: list[dict] = []
        self.driver = SimpleNamespace(name="driver")

    async def retrieve_episodes(self, reference_time, **kwargs):
        self.retrieved.append({"reference_time": reference_time, **kwargs})
        remaining = [
            uuid for uuid in self.saga_episodes.get(kwargs["saga"], []) if uuid not in self.removed
        ]
        return [SimpleNamespace(uuid=uuid) for uuid in remaining[: kwargs["last_n"]]]

    async def remove_episode(self, episode_uuid: str) -> None:
        self.removed.append(episode_uuid)


class FakeSaga:
    def __init__(self, name: str, deleted: list[str]) -> None:
        self.name = name
        self.deleted = deleted

    async def delete(self, driver) -> None:
        self.deleted.append(self.name)


async def test_forget_meeting_removes_every_saga_episode_and_the_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saga = meeting_saga("meeting-1")
    memory = FakeForgetMemory({saga: [f"e{index}" for index in range(5)], "meeting:other": ["x"]})
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
    memory = FakeForgetMemory({saga: ["stuck"]})

    async def remove_without_effect(episode_uuid: str) -> None:
        return None

    memory.remove_episode = remove_without_effect

    with pytest.raises(RuntimeError, match="stuck"):
        await forget_meeting(memory, group_id="team_alpha", meeting_id="meeting-1")


@pytest.mark.parametrize("group_id,allowed", [("team_alpha", True), ("team_beta", False)])
async def test_forget_episode_only_removes_episodes_in_the_persona_group(
    monkeypatch: pytest.MonkeyPatch, group_id: str, allowed: bool
) -> None:
    memory = FakeForgetMemory({})

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

    class NoteMemory:
        async def add_episode(self, **kwargs):
            added.append(kwargs)
            return SimpleNamespace(episode=SimpleNamespace(uuid="note-1"), edges=[])

    result = await remember_owner_note(
        NoteMemory(), group_id="team_alpha", text="  The Orchid demo moves to Friday.  "
    )

    assert result.episode.uuid == "note-1"
    [note] = added
    assert note["episode_body"] == "The Orchid demo moves to Friday."
    assert note["source"] == EpisodeType.text
    assert note["source_description"] == memory_module.OWNER_NOTE_SOURCE
    assert note["group_id"] == "team_alpha"
    assert note["name"].startswith("owner-note:")
    assert note["reference_time"].tzinfo is not None


async def test_recall_times_out_below_the_agent_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class HangingMemory(FakeMemory):
        async def search(self, query, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(memory_module, "RECALL_TIMEOUT_SECONDS", 0.01)
    _remember, recall = create_memory_tools(HangingMemory(), group_id="team_alpha")

    with pytest.raises(TimeoutError):
        await recall.ainvoke({"query": "agenda"})
    assert memory_module.RECALL_TIMEOUT_SECONDS < 60
