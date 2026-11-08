import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator

import fakeyou.memory.memory as memory_module
from fakeyou.config.settings import DEFAULTS, RecallSettings
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
    assert payload["excluded"] == {"superseded": 0, "missing_source": 0}
    assert memory.searched[0]["num_results"] == DEFAULTS.memory.recall.search_limit


async def test_recall_orders_facts_by_search_rank_before_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        f"e{index}": fake_episode(
            f"e{index}",
            "team_alpha",
            "Google Meet m1 transcript",
            datetime(2026, 9, 30, 9, index, tzinfo=UTC),
        )
        for index in range(7)
    }
    facts = [f"meeting-{index}" for index in range(7)]
    memory = make_graphiti(
        facts=facts, episodes={fact: [f"e{index}"] for index, fact in enumerate(facts)}
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "agenda"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        f"meeting-{index}" for index in range(DEFAULTS.memory.recall.result_limit)
    ]


async def test_recall_keeps_search_order_over_source_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "e0": fake_episode(
            "e0", "team_alpha", "Google Meet m0 transcript", datetime(2026, 9, 29, 9, tzinfo=UTC)
        ),
        "e1": fake_episode(
            "e1", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 9, tzinfo=UTC)
        ),
        "e2": fake_episode(
            "e2", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 10, tzinfo=UTC)
        ),
    }
    memory = make_graphiti(
        facts=["meeting-old", "meeting-oldest", "meeting-new"],
        episodes={"meeting-oldest": ["e0"], "meeting-new": ["e2"], "meeting-old": ["e1"]},
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "agenda"}))

    assert [fact["text"] for fact in payload["facts"]] == [
        "meeting-old",
        "meeting-oldest",
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
    assert payload["excluded"] == {"superseded": 0, "missing_source": 1}


async def test_recall_empty_returns_sentinel() -> None:
    memory = make_graphiti(facts=[])
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    assert await recall.ainvoke({"query": "agenda"}) == RECALL_EMPTY_RESULT
    assert RECALL_EMPTY_RESULT.strip() != ""


async def test_recall_merges_matching_facts_and_lists_newest_source_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
    newer = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "meet-b": fake_episode("meet-b", "team_alpha", "Google Meet m2 transcript", newer),
        "meet-a": fake_episode("meet-a", "team_alpha", "Google Meet m1 transcript", older),
    }
    memory = make_graphiti(
        facts=["Orchid demo moves to Friday.", "Orchid demo moves to  Friday."],
        episodes={
            "Orchid demo moves to Friday.": ["meet-a"],
            "Orchid demo moves to  Friday.": ["meet-b"],
        },
    )
    recall = recall_tool(monkeypatch, memory, stored)

    payload = json.loads(await recall.ainvoke({"query": "Orchid demo"}))

    assert len(payload["facts"]) == 1
    assert payload["facts"][0]["fact_ids"] == ["edge-0", "edge-1"]
    assert [source["episode_id"] for source in payload["facts"][0]["sources"]] == [
        "meet-b",
        "meet-a",
    ]
    assert {source["speaker_identity"] for source in payload["facts"][0]["sources"]} == {
        "unverified: may or may not be the owner or any named person"
    }


async def test_recall_excludes_superseded_foreign_and_missing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "live": fake_episode("live", "team_alpha", "Google Meet m1 transcript", moment),
        "old": fake_episode("old", "team_alpha", "Google Meet m2 transcript", moment),
        "foreign-episode": fake_episode(
            "foreign-episode", "team_beta", "Google Meet m3 transcript", moment
        ),
    }
    memory = make_graphiti(
        facts=[
            "Live fact.",
            "Stale fact.",
            "Foreign episode fact.",
            "Foreign edge fact.",
            "Missing episode fact.",
        ],
        episodes={
            "Live fact.": ["live"],
            "Stale fact.": ["old"],
            "Foreign episode fact.": ["foreign-episode"],
            "Foreign edge fact.": ["live"],
            "Missing episode fact.": ["gone"],
        },
        fact_groups={"Foreign edge fact.": "team_beta"},
        invalidated=["Stale fact."],
    )
    recall = recall_tool(monkeypatch, memory, stored)

    raw = await recall.ainvoke({"query": "fact"})
    payload = json.loads(raw)

    assert payload["schema"] == "memory_evidence/v1"
    assert [fact["text"] for fact in payload["facts"]] == ["Live fact."]
    assert payload["excluded"] == {"superseded": 1, "missing_source": 3}
    assert "foreign-episode" not in raw
    assert "team_beta" not in raw
    assert "gone" not in raw


async def test_recall_returns_empty_when_no_evidence_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = make_graphiti(
        facts=["Missing episode fact."], episodes={"Missing episode fact.": ["gone"]}
    )
    recall = recall_tool(monkeypatch, memory, {})

    assert await recall.ainvoke({"query": "fact"}) == RECALL_EMPTY_RESULT


async def test_recall_batches_episode_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    moment = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    stored = {
        "meet-b": fake_episode("meet-b", "team_alpha", "Google Meet m2 transcript", moment),
        "meet-a": fake_episode("meet-a", "team_alpha", "Google Meet m1 transcript", moment),
    }
    episode_calls: list[list[str]] = []

    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        del driver
        episode_calls.append(list(uuids))
        return [stored[uuid] for uuid in uuids if uuid in stored]

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    memory = make_graphiti(
        facts=["Alpha fact.", "Beta fact."],
        episodes={"Alpha fact.": ["meet-b", "meet-a"], "Beta fact.": ["meet-b"]},
    )
    [recall] = create_memory_tools(memory, group_id="team_alpha")

    await recall.ainvoke({"query": "fact"})

    assert [entry.get("search_filter") for entry in memory.searched] == [
        memory_module.CURRENT_FACTS_FILTER,
        None,
    ]
    assert episode_calls == [["meet-a", "meet-b"]]


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
