import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodicNode
from graphiti_core.search.search_config import EdgeReranker, EdgeSearchMethod

import fakeyou.memory.evidence as evidence_module
from fakeyou.config.settings import RecallSettings
from fakeyou.foundation.provenance import FactDict, MemoryEvidence
from fakeyou.memory.evidence import (
    CURRENT_FACTS_FILTER,
    PREFETCH_ADDITIONAL_EMPTY_EVIDENCE,
    PREFETCH_EMPTY_EVIDENCE,
    PREFETCH_UNAVAILABLE_EVIDENCE,
    create_memory_search,
    evidence_set,
    render_memory_evidence,
    search_evidence,
    unavailable_set,
)
from fakeyou.memory.persona import memory_group_for
from tests._memory_fakes import make_graphiti
from tests._memory_support import fake_episode
from tests._persona_support import profile

SOURCE_AT = datetime(2026, 9, 30, 9, tzinfo=UTC)


def patch_episodes(
    monkeypatch: pytest.MonkeyPatch,
    memory: Any,
    stored: dict[str, Any],
    states: dict[str, str] | None = None,
) -> None:
    async def get_by_uuids(driver: Any, uuids: list[str]) -> list[Any]:
        assert driver is memory.driver
        return [stored[uuid] for uuid in uuids if uuid in stored]

    async def note_states(
        _memory: Any, *, group_id: str, episode_uuids: list[str]
    ) -> dict[str, str]:
        assert group_id == "team_alpha"
        return {uuid: (states or {}).get(uuid, "active") for uuid in episode_uuids}

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    monkeypatch.setattr(evidence_module, "read_note_states", note_states)


def found_set(text: str, *, fact_id: str = "f1") -> MemoryEvidence:
    fact: FactDict = {"fact_ids": [fact_id], "text": text, "sources": []}
    return {
        "outcome": "found",
        "facts": [fact],
        "excluded": {"pending": 0, "superseded": 0, "missing_source": 0},
    }


def empty_set() -> MemoryEvidence:
    return {
        "outcome": "empty",
        "facts": [],
        "excluded": {"pending": 0, "superseded": 0, "missing_source": 0},
    }


async def test_search_evidence_returns_structured_found_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {"e0": fake_episode("e0", "team_alpha", "owner note", SOURCE_AT)}
    memory = make_graphiti(facts=["Budget approved"], episodes={"Budget approved": ["e0"]})
    patch_episodes(monkeypatch, memory, stored)

    result = await search_evidence(memory, "budget", group_id="team_alpha", recall=RecallSettings())
    rendered = evidence_set(result, result_limit=5)

    assert rendered["outcome"] == "found"
    assert rendered["facts"][0]["text"] == "Budget approved"
    assert rendered["facts"][0]["sources"][0]["kind"] == "owner_note"
    assert rendered["facts"][0]["sources"][0] == {
        "episode_id": "e0",
        "kind": "owner_note",
        "label": "owner note",
        "source_at": "2026-09-30T09:00:00+00:00",
        "recorded_at": "2026-09-30T09:00:00+00:00",
        "time_granularity": "document",
        "speaker_verified": False,
        "speaker_identity": "owner",
    }
    assert rendered["excluded"] == {"pending": 0, "superseded": 0, "missing_source": 0}
    assert json.loads(render_memory_evidence([rendered], 1200))["schema"] == "memory_evidence/v1"


async def test_search_evidence_without_matching_facts_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = make_graphiti(facts=[])
    patch_episodes(monkeypatch, memory, {})

    result = await search_evidence(memory, "budget", group_id="team_alpha", recall=RecallSettings())

    assert result.outcome == "empty"
    assert result.facts == ()
    assert evidence_set(result, result_limit=5) == empty_set()


async def test_search_evidence_counts_pending_and_superseded_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "live": fake_episode("live", "team_alpha", "owner note", SOURCE_AT),
        "waiting": fake_episode("waiting", "team_alpha", "owner note", SOURCE_AT),
        "old": fake_episode("old", "team_alpha", "owner note", SOURCE_AT),
    }
    memory = make_graphiti(
        facts=["Live fact.", "Waiting fact.", "Old fact.", "Lost fact."],
        episodes={
            "Live fact.": ["live"],
            "Waiting fact.": ["waiting"],
            "Old fact.": ["old"],
            "Lost fact.": ["missing"],
        },
    )
    patch_episodes(monkeypatch, memory, stored, states={"waiting": "pending", "old": "superseded"})

    result = await search_evidence(memory, "fact", group_id="team_alpha", recall=RecallSettings())
    rendered = evidence_set(result, result_limit=5)

    assert [fact["text"] for fact in rendered["facts"]] == ["Live fact."]
    assert rendered["excluded"] == {"pending": 1, "superseded": 1, "missing_source": 1}


async def test_search_evidence_lets_search_failures_escape() -> None:
    memory = make_graphiti(search_error=RuntimeError("graph down"))

    with pytest.raises(RuntimeError, match="graph down"):
        await search_evidence(memory, "budget", group_id="team_alpha", recall=RecallSettings())


async def test_create_memory_search_maps_failure_to_unavailable() -> None:
    memory = make_graphiti(search_error=RuntimeError("graph down"))
    search = create_memory_search(memory, recall=RecallSettings())

    result = await search("budget", owner_id="owner-1", twin_id="twin-1")

    assert result == unavailable_set()
    assert memory.searched[0]["group_ids"] == [memory_group_for("owner-1", "twin-1")]


async def test_create_memory_search_returns_the_structured_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    group = memory_group_for("owner-1", "twin-1")
    stored = {"m1": fake_episode("m1", group, "Google Meet m1 transcript", SOURCE_AT)}
    memory = make_graphiti(facts=["Launch is Thursday"], episodes={"Launch is Thursday": ["m1"]})

    async def get_by_uuids(_driver: Any, uuids: list[str]) -> list[Any]:
        return [stored[uuid] for uuid in uuids if uuid in stored]

    monkeypatch.setattr(EpisodicNode, "get_by_uuids", get_by_uuids)
    search = create_memory_search(memory, recall=RecallSettings())

    result = await search("launch", owner_id="owner-1", twin_id="twin-1")

    assert result["outcome"] == "found"
    assert result["facts"][0]["sources"][0]["kind"] == "meeting_transcript"
    assert result["facts"][0]["sources"][0]["time_granularity"] == "episode_window"


def test_unavailable_set_is_the_frozen_shape() -> None:
    assert dict(unavailable_set()) == {
        "outcome": "unavailable",
        "facts": [],
        "excluded": {"pending": 0, "superseded": 0, "missing_source": 0},
    }


def test_renderer_keeps_last_three_sets_and_trims_older_to_budget() -> None:
    older = found_set(text="x" * 400)
    rendered = render_memory_evidence(
        [found_set(text="a"), older, found_set(text="b"), found_set(text="c")], 200
    )
    lines = rendered.splitlines()

    assert len(lines) == 3
    assert all(len(line) <= 200 for line in lines[:-1])
    assert json.loads(lines[-1])["facts"][0]["text"] == "c"
    assert all(json.loads(line) for line in lines)
    assert json.loads(lines[0])["facts"][0]["text"].startswith("x")


def test_renderer_drops_tail_facts_of_older_sets_before_cutting_text() -> None:
    older: MemoryEvidence = {
        "outcome": "found",
        "facts": [
            {"fact_ids": ["f1"], "text": "first " * 10, "sources": []},
            {"fact_ids": ["f2"], "text": "second " * 10, "sources": []},
        ],
        "excluded": {"pending": 0, "superseded": 0, "missing_source": 0},
    }

    first, latest = render_memory_evidence([older, found_set("now")], 240).splitlines()

    assert [fact["fact_ids"] for fact in json.loads(first)["facts"]] == [["f1"]]
    assert json.loads(first)["facts"][0]["text"] == "first " * 10
    assert json.loads(latest)["facts"][0]["text"] == "now"


def test_renderer_never_trims_the_latest_set() -> None:
    latest = render_memory_evidence([found_set("y" * 500)], 100)

    assert json.loads(latest)["facts"][0]["text"] == "y" * 500


async def test_search_evidence_maps_screen_and_chat_episodes_to_their_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "s1": fake_episode("s1", "team_alpha", "Google Meet m1 shared screen", SOURCE_AT),
        "c1": fake_episode("c1", "team_alpha", "Google Meet m1 chat", SOURCE_AT),
        "o1": fake_episode("o1", "team_alpha", "imported slides", SOURCE_AT),
    }
    memory = make_graphiti(
        facts=["The slide shows Q3 revenue.", "Send the deck later.", "Imported fact."],
        episodes={
            "The slide shows Q3 revenue.": ["s1"],
            "Send the deck later.": ["c1"],
            "Imported fact.": ["o1"],
        },
    )
    patch_episodes(monkeypatch, memory, stored)

    result = await search_evidence(memory, "deck", group_id="team_alpha", recall=RecallSettings())
    rendered = evidence_set(result, result_limit=5)

    sources = {
        source["episode_id"]: (source["kind"], source["time_granularity"])
        for fact in rendered["facts"]
        for source in fact["sources"]
    }
    assert sources == {
        "s1": ("meeting_screen", "episode_window"),
        "c1": ("meeting_chat", "episode_window"),
        "o1": ("other", "document"),
    }
    assert json.loads(render_memory_evidence([rendered], 1200))["schema"] == "memory_evidence/v1"


def test_renderer_speaks_prose_for_latest_empty_or_unavailable() -> None:
    assert render_memory_evidence([empty_set()], 1200) == PREFETCH_EMPTY_EVIDENCE
    assert render_memory_evidence([unavailable_set()], 1200) == PREFETCH_UNAVAILABLE_EVIDENCE
    assert render_memory_evidence([], 1200) == ""


def test_renderer_drops_older_empty_sets_and_marks_additional_empty() -> None:
    lines = render_memory_evidence(
        [unavailable_set(), found_set("kept"), empty_set(), empty_set()], 1200
    ).splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["facts"][0]["text"] == "kept"
    assert lines[1] == PREFETCH_ADDITIONAL_EMPTY_EVIDENCE


@pytest.mark.parametrize(
    ("owner_id", "twin_id"),
    [("owner-a", "twin-a"), ("owner-a", "twin-b"), ("owner_9", "local_twin")],
)
def test_memory_group_for_matches_the_profile_property(owner_id: str, twin_id: str) -> None:
    assert (
        memory_group_for(owner_id, twin_id)
        == profile(owner_id=owner_id, twin_id=twin_id).memory_group
    )


def recipe_search_double(memory: Any) -> Any:
    memory.recipe_calls = []

    async def search_(query: str, *, config: Any, group_ids: list[str], search_filter: Any) -> Any:
        memory.recipe_calls.append({"config": config, "search_filter": search_filter})
        if search_filter is None:
            edges = await memory.search(query, group_ids=group_ids, num_results=config.limit)
        else:
            edges = await memory.search(
                query, group_ids=group_ids, num_results=config.limit, search_filter=search_filter
            )
        return SimpleNamespace(edges=edges)

    return search_


async def test_search_recipe_backend_keeps_excluded_counts_honest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = {
        "live": fake_episode(
            "live", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 30, 9, tzinfo=UTC)
        ),
        "old": fake_episode(
            "old", "team_alpha", "Google Meet m1 transcript", datetime(2026, 9, 29, 9, tzinfo=UTC)
        ),
    }
    memory = make_graphiti(
        facts=["Budget approved", "Budget rejected"],
        episodes={"Budget approved": ["live"], "Budget rejected": ["old"]},
        invalidated=["Budget rejected"],
    )
    patch_episodes(monkeypatch, memory, stored)
    memory.search_ = recipe_search_double(memory)

    standard = await search_evidence(
        memory, "budget", group_id="team_alpha", recall=RecallSettings()
    )
    recipe = await search_evidence(
        memory, "budget", group_id="team_alpha", recall=RecallSettings(), backend="search_recipe"
    )

    assert evidence_set(recipe, result_limit=5) == evidence_set(standard, result_limit=5)
    assert evidence_set(recipe, result_limit=5)["excluded"]["superseded"] == 1


async def test_search_recipe_backend_declares_its_two_call_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = make_graphiti(facts=["Budget approved"])
    patch_episodes(monkeypatch, memory, {})
    memory.search_ = recipe_search_double(memory)
    recall = RecallSettings()

    await search_evidence(
        memory, "budget", group_id="team_alpha", recall=recall, backend="search_recipe"
    )

    assert [call["search_filter"] for call in memory.recipe_calls] == [CURRENT_FACTS_FILTER, None]
    for call in memory.recipe_calls:
        edge_config = call["config"].edge_config
        assert edge_config.search_methods == [
            EdgeSearchMethod.bm25,
            EdgeSearchMethod.cosine_similarity,
        ]
        assert edge_config.reranker is EdgeReranker.cross_encoder
        assert call["config"].limit == recall.search_limit
    assert memory.recipe_calls[0]["config"].node_config is None
