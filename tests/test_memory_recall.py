import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator

import fakeyou.memory.memory as memory_module
from fakeyou.config.settings import DEFAULTS, RecallSettings
from fakeyou.memory import owner_notes
from fakeyou.memory.evidence import RECALL_EMPTY_RESULT
from fakeyou.memory.memory import create_memory_tools
from tests._memory_fakes import make_graphiti
from tests._memory_support import fake_episode, recall_tool


async def test_recall_caps_results(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {
        f"e{index}": fake_episode(
            f"e{index}",
            "team_alpha",
            "Google Meet m1 transcript",
            datetime(2026, 9, 30, 9, index, tzinfo=UTC),
        )
        for index in range(8)
    }
    memory = make_graphiti(
        facts=[f"fact-{index}" for index in range(8)],
        episodes={f"fact-{index}": [f"e{index}"] for index in range(8)},
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "agenda"}))

    assert payload["schema"] == "memory_evidence/v1"
    assert len(payload["facts"]) == DEFAULTS.memory.recall.result_limit
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}
    assert memory.searched[0]["num_results"] == DEFAULTS.memory.recall.search_limit


async def test_recall_orders_facts_by_search_rank_and_pins_top_owner_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        f"e{index}": fake_episode(
            f"e{index}",
            "team_alpha",
            "Google Meet m1 transcript",
            datetime(2026, 9, 30, 9, index, tzinfo=UTC),
        )
        for index in range(6)
    }
    stored["owner"] = fake_episode(
        "owner", "team_alpha", "owner note", datetime(2026, 9, 29, 9, tzinfo=UTC)
    )
    facts = [f"meeting-{index}" for index in range(6)] + ["owner-fact"]
    episodes = {fact: [f"e{index}"] for index, fact in enumerate(facts) if fact != "owner-fact"}
    episodes["owner-fact"] = ["owner"]
    memory = make_graphiti(facts=facts, episodes=episodes)
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "agenda"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "meeting-0",
        "meeting-1",
        "meeting-2",
        "meeting-3",
        "owner-fact",
    ]


async def test_recall_keeps_search_order_when_owner_fact_already_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "owner": fake_episode(
            "owner", "team_alpha", "owner note", datetime(2026, 9, 29, 9, tzinfo=UTC)
        ),
        "e1": fake_episode(
            "e1", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 9, tzinfo=UTC)
        ),
        "e2": fake_episode(
            "e2", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 10, tzinfo=UTC)
        ),
    }
    memory = make_graphiti(
        facts=["meeting-old", "owner-fact", "meeting-new"],
        episodes={"owner-fact": ["owner"], "meeting-new": ["e2"], "meeting-old": ["e1"]},
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "agenda"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "meeting-old",
        "owner-fact",
        "meeting-new",
    ]


async def test_recall_merges_identical_facts_before_applying_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "ea": fake_episode(
            "ea", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 29, 9, tzinfo=UTC)
        ),
        "eb": fake_episode(
            "eb", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 9, tzinfo=UTC)
        ),
    }
    memory = make_graphiti(
        facts=["Alpha launch Friday.", "Alpha  launch Friday.", "Beta launch Monday."],
        episodes={
            "Alpha launch Friday.": ["ea"],
            "Alpha  launch Friday.": ["eb"],
            "Beta launch Monday.": ["eb"],
        },
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "launch"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "Alpha launch Friday.",
        "Beta launch Monday.",
    ]
    assert payload["facts"][0]["fact_ids"] == ["edge-0", "edge-1"]
    assert {source["episode_id"] for source in payload["facts"][0]["sources"]} == {"ea", "eb"}


def test_current_facts_filter_excludes_invalidated_and_expired_edges() -> None:
    search_filter = memory_module.CURRENT_FACTS_FILTER

    assert search_filter.invalid_at is not None
    assert search_filter.expired_at is not None
    for groups in (search_filter.invalid_at, search_filter.expired_at):
        assert [[item.comparison_operator for item in group] for group in groups] == [
            [ComparisonOperator.is_null]
        ]


async def test_recall_labels_each_fact_with_its_episode_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "e1": fake_episode(
            "e1",
            "team_alpha",
            "Google Meet m1 transcript",
            datetime(2026, 9, 23, 10, tzinfo=UTC),
        ),
        "e2": fake_episode(
            "e2",
            "team_alpha",
            "Google Meet m2 transcript",
            datetime(2026, 10, 1, 0, 30, tzinfo=timezone(timedelta(hours=7))),
            datetime(2026, 10, 1, 1, 5, tzinfo=UTC),
        ),
    }
    memory = make_graphiti(
        facts=["Demo moves to Thursday", "Budget is final", "Venue is Hall A"],
        episodes={
            "Demo moves to Thursday": ["e2", "e1"],
            "Budget is final": ["e2"],
            "Venue is Hall A": ["missing"],
        },
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "demo"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "Demo moves to Thursday",
        "Budget is final",
    ]
    assert payload["facts"][0]["sources"] == [
        {
            "episode_id": "e2",
            "kind": "meeting_transcript",
            "label": "Google Meet m2 transcript",
            "source_at": "2026-09-30T17:30:00+00:00",
            "recorded_at": "2026-10-01T01:05:00+00:00",
            "time_granularity": "episode_window",
            "speaker_verified": False,
            "speaker_identity": "unverified: may or may not be the owner or any named person",
        },
        {
            "episode_id": "e1",
            "kind": "meeting_transcript",
            "label": "Google Meet m1 transcript",
            "source_at": "2026-09-23T10:00:00+00:00",
            "recorded_at": "2026-09-23T10:00:00+00:00",
            "time_granularity": "episode_window",
            "speaker_verified": False,
            "speaker_identity": "unverified: may or may not be the owner or any named person",
        },
    ]
    assert payload["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 1}


async def test_recall_empty_returns_sentinel() -> None:
    memory = make_graphiti(facts=[])
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "agenda"}) == RECALL_EMPTY_RESULT
    assert RECALL_EMPTY_RESULT.strip() != ""


async def test_recall_keeps_owner_note_source_when_text_matches_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded = datetime(2026, 10, 2, 8, tzinfo=UTC)
    stored = {
        "note-a": fake_episode(
            "note-a", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, newer, recorded
        ),
        "meet-a": fake_episode("meet-a", "team_alpha", "Google Meet m1 transcript", older),
    }
    memory = make_graphiti(
        facts=["Orchid demo moves to Friday.", "Orchid demo moves to  Friday."],
        episodes={
            "Orchid demo moves to Friday.": ["meet-a"],
            "Orchid demo moves to  Friday.": ["note-a"],
        },
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "Orchid demo"}))

    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["edge-0", "edge-1"]
    assert [source["episode_id"] for source in payload["facts"][0]["sources"]] == [
        "note-a",
        "meet-a",
    ]
    assert payload["facts"][0]["sources"][0]["kind"] == "owner_note"
    assert [source["speaker_identity"] for source in payload["facts"][0]["sources"]] == [
        "owner",
        "unverified: may or may not be the owner or any named person",
    ]
    assert {source["source_at"] for source in payload["facts"][0]["sources"]} == {
        older.isoformat(),
        newer.isoformat(),
    }


async def test_recall_excludes_pending_superseded_foreign_and_missing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "live": fake_episode("live", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "old": fake_episode("old", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "fixed": fake_episode("fixed", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "waiting": fake_episode("waiting", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "meet-x": fake_episode("meet-x", "team_alpha", "Google Meet m9 transcript", moment),
        "foreign-episode": fake_episode(
            "foreign-episode", "team_beta", owner_notes.OWNER_NOTE_SOURCE, moment
        ),
    }
    states = {"live": "active", "old": "superseded", "fixed": "active", "waiting": "pending"}

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is memory.driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def scripted_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        assert group_id == "team_alpha"
        return {uuid: states[uuid] for uuid in episode_uuids}

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", scripted_states)
    memory = make_graphiti(
        facts=[
            "Live note fact.",
            "Stale note fact.",
            "Fixed note fact.",
            "Waiting note fact.",
            "Stale repeated fact.",
            "Foreign episode fact.",
            "Foreign edge fact.",
            "Missing episode fact.",
        ],
        episodes={
            "Live note fact.": ["live"],
            "Stale note fact.": ["old"],
            "Fixed note fact.": ["old", "fixed"],
            "Waiting note fact.": ["waiting"],
            "Stale repeated fact.": ["old", "meet-x"],
            "Foreign episode fact.": ["foreign-episode"],
            "Foreign edge fact.": ["live"],
            "Missing episode fact.": ["gone"],
        },
        fact_groups={"Foreign edge fact.": "team_beta"},
    )
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    raw = await recall.ainvoke({"query": "note fact"})
    payload = json.loads(raw)

    assert payload["schema"] == "memory_evidence/v1"
    assert [fact["text"] for fact in payload["facts"]] == [
        "Live note fact.",
        "Fixed note fact.",
    ]
    assert [source["episode_id"] for source in payload["facts"][1]["sources"]] == ["fixed"]
    assert payload["excluded"] == {"pending": 1, "superseded": 2, "missing_source": 3}
    assert "foreign-episode" not in raw
    assert "team_beta" not in raw
    assert "gone" not in raw
    assert "waiting" not in raw
    assert "meet-x" not in raw


async def test_recall_returns_empty_when_no_evidence_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "waiting": fake_episode("waiting", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
    }

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        del driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def pending_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        del group_id
        return dict.fromkeys(episode_uuids, "pending")

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", pending_states)
    memory = make_graphiti(
        facts=["Waiting note fact.", "Missing episode fact."],
        episodes={"Waiting note fact.": ["waiting"], "Missing episode fact.": ["gone"]},
    )
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "note fact"}) == RECALL_EMPTY_RESULT


async def test_recall_status_lookup_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "note-a": fake_episode("note-a", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
    }

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        del driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def broken_states(_memory: Any, **kwargs: Any) -> dict[str, str]:
        del kwargs
        raise RuntimeError("status store offline")

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", broken_states)
    memory = make_graphiti(facts=["Live note fact."], episodes={"Live note fact.": ["note-a"]})
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    with pytest.raises(RuntimeError, match="status store offline"):
        await recall.ainvoke({"query": "note fact"})


async def test_recall_batches_episode_and_status_lookups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "note-a": fake_episode("note-a", "team_alpha", owner_notes.OWNER_NOTE_SOURCE, moment),
        "meet-a": fake_episode("meet-a", "team_alpha", "Google Meet m1 transcript", moment),
    }
    episode_calls: list[list[str]] = []
    state_calls: list[list[str]] = []

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        del driver
        episode_calls.append(list(uuids))
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def active_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        del group_id
        state_calls.append(list(episode_uuids))
        return dict.fromkeys(episode_uuids, "active")

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(memory_module, "read_note_states", active_states)
    memory = make_graphiti(
        facts=["Alpha fact.", "Beta fact."],
        episodes={"Alpha fact.": ["note-a", "meet-a"], "Beta fact.": ["note-a"]},
    )
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    await recall.ainvoke({"query": "fact"})

    assert [entry.get("search_filter") for entry in memory.searched] == [
        memory_module.CURRENT_FACTS_FILTER,
        None,
    ]
    assert episode_calls == [["meet-a", "note-a"]]
    assert state_calls == [["note-a"]]


async def test_recall_times_out_with_its_configured_budget() -> None:
    [recall] = create_memory_tools(
        make_graphiti(search_gate=asyncio.Event()),
        group_id="team_alpha",
        recall=RecallSettings(timeout_seconds=0.01),
    )

    with pytest.raises(TimeoutError):
        await recall.ainvoke({"query": "agenda"})


def test_default_recall_budget_fits_inside_the_reply_deadline() -> None:
    assert DEFAULTS.memory.recall.timeout_seconds < DEFAULTS.conversation.response_timeout_seconds


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
    [recall] = create_memory_tools(memory, group_id="team_alpha")

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
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "demo"}) == RECALL_EMPTY_RESULT
