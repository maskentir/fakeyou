from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode, SagaNode

import fakeyou.memory.memory as memory_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory import owner_notes
from fakeyou.memory.memory import forget_episode, forget_meeting, meeting_saga
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


@pytest.mark.parametrize(("group_id", "allowed"), [("team_alpha", True), ("team_beta", False)])
async def test_forget_episode_only_removes_episodes_in_the_persona_group(
    monkeypatch: pytest.MonkeyPatch, group_id: str, *, allowed: bool
) -> None:
    memory = make_graphiti(saga_episodes={})

    async def get_by_uuid(driver: Any, uuid: str) -> SimpleNamespace:
        del driver
        return SimpleNamespace(
            uuid=uuid, group_id=group_id, source_description="Google Meet m1 transcript"
        )

    monkeypatch.setattr(EpisodicNode, "get_by_uuid", get_by_uuid)

    if allowed:
        await forget_episode(memory, group_id="team_alpha", episode_uuid="e1")
        assert memory.removed == ["e1"]
    else:
        with pytest.raises(FakeYouError, match="not in this persona") as caught:
            await forget_episode(memory, group_id="team_alpha", episode_uuid="e1")
        assert caught.value.code is ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY
        assert memory.removed == []


async def test_forget_episode_routes_owner_notes_through_chain_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = make_graphiti(saga_episodes={})
    calls: list[dict[str, Any]] = []

    async def get_by_uuid(driver: Any, uuid: str) -> SimpleNamespace:
        del driver
        label = owner_notes.OWNER_NOTE_SOURCE if uuid == "note-1" else "Google Meet m1 transcript"
        return SimpleNamespace(uuid=uuid, group_id="team_alpha", source_description=label)

    async def guarded_forget(_memory: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    assert memory_module.forget_owner_note is owner_notes.forget_owner_note
    monkeypatch.setattr(EpisodicNode, "get_by_uuid", get_by_uuid)
    monkeypatch.setattr(memory_module, "forget_owner_note", guarded_forget)

    await forget_episode(memory, group_id="team_alpha", episode_uuid="note-1")
    await forget_episode(memory, group_id="team_alpha", episode_uuid="meet-1")

    assert calls == [{"group_id": "team_alpha", "episode_uuid": "note-1"}]
    assert memory.removed == ["meet-1"]


def test_owner_note_writes_live_in_owner_notes_without_a_memory_reexport() -> None:
    assert not hasattr(memory_module, "remember_owner_note")
    assert owner_notes.OWNER_NOTE_SOURCE == "owner note"
    assert callable(owner_notes.remember_owner_note)
