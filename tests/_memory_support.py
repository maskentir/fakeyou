from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode

import fakeyou.memory.memory as memory_module
from fakeyou.config.settings import MemorySettings
from fakeyou.memory.memory import create_memory_tools


def memory_settings() -> MemorySettings:
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


def fake_episode(
    uuid: str,
    group_id: str,
    label: str,
    source_at: datetime,
    recorded_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        uuid=uuid,
        group_id=group_id,
        source_description=label,
        valid_at=source_at,
        created_at=recorded_at or source_at,
    )


def recall_tool(monkeypatch: pytest.MonkeyPatch, memory: Any, episodes: dict[str, Any]) -> Any:
    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is memory.driver
        return [episodes[uuid] for uuid in uuids if uuid in episodes]

    async def active_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        assert group_id == "team_alpha"
        return dict.fromkeys(episode_uuids, "active")

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", active_states)
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")
    return recall
