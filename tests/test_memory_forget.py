from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core.nodes import SagaNode

import fakeyou.memory.memory as memory_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.memory import forget_meeting, meeting_saga
from tests._memory_fakes import FakeSaga, make_graphiti


async def test_forget_meeting_removes_every_saga_episode_and_the_saga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saga = meeting_saga("meeting-1")
    memory = make_graphiti(
        saga_episodes={saga: [f"e{index}" for index in range(5)], "meeting:other": ["x"]}
    )
    deleted: list[str] = []
    saga_groups: list[list[str]] = []

    async def get_by_group_ids(driver: Any, group_ids: list[str]) -> list[Any]:
        del driver
        saga_groups.append(group_ids)
        return [FakeSaga(saga, deleted), FakeSaga("meeting:other", deleted)]

    monkeypatch.setattr(memory_module, "FORGET_BATCH_SIZE", 2)
    monkeypatch.setattr(SagaNode, "get_by_group_ids", get_by_group_ids)

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
    del monkeypatch
    saga = meeting_saga("meeting-1")
    memory = make_graphiti(saga_episodes={saga: ["stuck"]})

    async def remove_without_effect(episode_uuid: str) -> None:
        del episode_uuid
        return None

    memory.remove_episode = remove_without_effect

    with pytest.raises(FakeYouError, match="stuck") as caught:
        await forget_meeting(memory, group_id="team_alpha", meeting_id="meeting-1")
    assert caught.value.code is ErrorCode.MEMORY_FORGET_INCOMPLETE
