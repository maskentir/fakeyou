import json
from datetime import UTC, datetime
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode

import fakeyou.memory.memory as memory_module
from fakeyou.memory import owner_notes
from fakeyou.memory.memory import create_memory_tools
from tests._memory_fakes import make_graphiti
from tests._memory_support import fake_episode, recall_tool


def _owner_rumor_memory(
    monkeypatch: pytest.MonkeyPatch, *, owner_state: str, invalidated: tuple[str, ...]
) -> Any:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "note": fake_episode("note", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "meet": fake_episode("meet", "team_alpha", "Google Meet m3 transcript", moment),
    }

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        del driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def scripted_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        del group_id
        return {uuid: owner_state for uuid in episode_uuids}

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", scripted_states)
    return make_graphiti(
        facts=[
            "Owner moved the demo to Friday.",
            "Unverified speaker said the demo moves to November.",
        ],
        episodes={
            "Owner moved the demo to Friday.": ["note"],
            "Unverified speaker said the demo moves to November.": ["meet"],
        },
        invalidated=invalidated,
    )


async def test_participant_claim_cannot_invalidate_an_active_owner_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _owner_rumor_memory(
        monkeypatch, owner_state="active", invalidated=("Owner moved the demo to Friday.",)
    )
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    payload = json.loads(await recall.ainvoke({"query": "demo"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "Unverified speaker said the demo moves to November.",
        "Owner moved the demo to Friday.",
    ]
    assert [entry.get("search_filter") for entry in memory.searched] == [
        memory_module.CURRENT_FACTS_FILTER,
        None,
    ]


async def test_invalidated_facts_without_an_active_owner_note_stay_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _owner_rumor_memory(
        monkeypatch,
        owner_state="superseded",
        invalidated=(
            "Owner moved the demo to Friday.",
            "Unverified speaker said the demo moves to November.",
        ),
    )
    _remember, recall = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "demo"}) == memory_module.RECALL_EMPTY_RESULT


async def test_short_remember_meeting_fact_buffers_until_two_facts_or_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = make_graphiti()
    remember, _ = create_memory_tools(memory, group_id="team_alpha")

    buffered = await remember.ainvoke({"fact": "Budi: The demo moved to Thursday."})

    assert buffered == "Buffered meeting fact."
    assert memory.added == []
    stored = await remember.ainvoke({"fact": "Joko: The client report is ready."})

    assert stored == "Remembered meeting fact."
    assert len(memory.added) == 1
    [episode] = memory.added
    assert "Budi: The demo moved to Thursday." in episode["episode_body"]
    assert "Budi stated this during the meeting." in episode["episode_body"]
    assert "Joko: The client report is ready." in episode["episode_body"]
    assert episode["source_description"].endswith("transcript")
    assert episode["group_id"] == "team_alpha"
    recall = recall_tool(
        monkeypatch,
        make_graphiti(facts=["Demo moved"], episodes={"Demo moved": ["e1"]}),
        {
            "e1": fake_episode(
                "e1",
                "team_alpha",
                episode["source_description"],
                datetime(2026, 9, 30, 9, tzinfo=UTC),
            )
        },
    )
    payload = json.loads(await recall.ainvoke({"query": "demo"}))
    assert [source["kind"] for source in payload["facts"][0]["sources"]] == ["meeting_transcript"]

    flushed_memory = make_graphiti()
    remember_flushed, _ = create_memory_tools(flushed_memory, group_id="team_alpha")
    await remember_flushed.ainvoke({"fact": "Nadia: The notes are sent."})

    assert flushed_memory.added == []
    outcome = await remember_flushed.ainvoke({"fact": "", "flush": True})

    assert outcome == "Remembered meeting fact."
    assert len(flushed_memory.added) == 1
    assert "Nadia stated this during the meeting." in flushed_memory.added[0]["episode_body"]


async def test_remember_meeting_fact_flushes_buffer_before_long_facts() -> None:
    memory = make_graphiti()
    remember, recall_tool = create_memory_tools(memory, group_id="team_alpha")
    long_fact = (
        "Budi: The Thursday launch stays on schedule and the rollout checklist "
        "is complete for all regions before the client demo this week."
    )

    assert len(long_fact) >= 120
    await remember.ainvoke({"fact": "Joko: The client report is ready."})
    stored = await remember.ainvoke({"fact": long_fact})

    assert stored == "Remembered meeting fact."
    assert len(memory.added) == 2
    assert "Joko: The client report is ready." in memory.added[0]["episode_body"]
    assert "Budi: The Thursday launch stays on schedule" in memory.added[1]["episode_body"]
    outcome = await remember.ainvoke({"fact": "", "flush": True})

    assert outcome == "Remembered meeting fact."
    assert len(memory.added) == 2


async def test_remember_meeting_fact_ignores_blank_facts_and_empty_flush() -> None:
    memory = make_graphiti()
    remember, recall_tool = create_memory_tools(memory, group_id="team_alpha")

    assert await remember.ainvoke({"fact": "   "}) == "Buffered meeting fact."
    assert await remember.ainvoke({"fact": "", "flush": True}) == "Remembered meeting fact."
    assert memory.added == []
