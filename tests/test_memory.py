from types import SimpleNamespace

import pytest

import fakeyou.memory as memory_module
from fakeyou.memory import (
    Neo4jMemoryConfig,
    create_memory_tools,
    ensure_indexes,
    open_graphiti_memory,
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
        "num_results": memory_module.RECALL_RESULT_LIMIT,
    }


class FakeGraphiti:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def build_indices_and_constraints(self) -> None:
        self.events.append("build")

    async def close(self) -> None:
        self.events.append("close")


def _config() -> Neo4jMemoryConfig:
    return Neo4jMemoryConfig(uri="bolt://localhost:7687", user="neo4j", password="p", api_key="k")


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
    def __init__(self, facts: list[str]) -> None:
        super().__init__()
        self.facts = facts

    async def search(self, query, **kwargs):
        self.searched.append({"query": query, **kwargs})
        return [SimpleNamespace(fact=fact) for fact in self.facts]


async def test_recall_caps_results() -> None:
    memory = FakeSearchMemory([f"fact-{index}" for index in range(8)])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    result = await recall.ainvoke({"query": "agenda"})

    assert result.splitlines() == [f"fact-{index}" for index in range(5)]
    assert memory.searched[0]["num_results"] == memory_module.RECALL_RESULT_LIMIT


async def test_recall_empty_returns_sentinel() -> None:
    memory = FakeSearchMemory([])
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "agenda"}) == memory_module.RECALL_EMPTY_RESULT
    assert memory_module.RECALL_EMPTY_RESULT.strip() != ""
