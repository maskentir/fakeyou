from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodeType, SagaNode

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.ingestion import screen_episode_body, screen_episode_name, screen_windows
from fakeyou.memory.memory import forget_meeting, meeting_saga
from fakeyou.persistence.record import RecordEntry, ScreenEntry
from tests._ingestion_support import run, said, shown
from tests._memory_fakes import make_graphiti

SHOWN_AT = datetime(2026, 9, 30, 9, 5, tzinfo=UTC)


def revenue_slide(ref: str = "screen-1") -> ScreenEntry:
    return shown(
        ref,
        "Q3 revenue",
        shown_at=SHOWN_AT,
        key_points=("Revenue is up",),
        figures=("12%",),
        visuals=("bar chart",),
        text="Quarterly revenue grew twelve percent.",
    )


def test_screen_episode_name_is_indexed_per_meeting() -> None:
    assert screen_episode_name("meeting-1", 3) == "meeting-1:s00003"


def test_screen_episode_body_reads_like_prose_and_skips_empty_parts() -> None:
    assert screen_episode_body(revenue_slide()) == "\n".join(
        (
            "Shared screen titled 'Q3 revenue' shown by Rina (slide).",
            "Key point: Revenue is up",
            "Figure: 12%",
            "Visual: bar chart",
            "Screen text: Quarterly revenue grew twelve percent.",
        )
    )
    assert (
        screen_episode_body(shown("screen-2", "Roadmap", presenter=None, content="document"))
        == "Shared screen titled 'Roadmap' (document)."
    )


def test_screen_windows_keep_the_latest_reading_per_ref_in_first_appearance_order() -> None:
    entries: list[RecordEntry] = [
        shown("screen-1", "Draft"),
        said(1, "Next slide."),
        shown("screen-2", "Roadmap"),
        shown("screen-1", "Final"),
        shown("  ", "Untracked"),
    ]

    assert [(entry.ref, entry.title) for entry in screen_windows(entries)] == [
        ("screen-1", "Final"),
        ("screen-2", "Roadmap"),
    ]


async def test_ingest_screen_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    entries = [said(0, "Look at the revenue slide please."), revenue_slide()]

    result, rows = await run(memory, entries)

    assert result.screen_windows == 1
    assert result.screen_uuids == {0: "uuid-meeting-1:s00000"}
    assert result.completed
    [screen] = [episode for episode in memory.added if episode["name"] == "meeting-1:s00000"]
    assert screen["episode_body"] == screen_episode_body(revenue_slide())
    assert screen["source_description"] == "Google Meet meeting-1 shared screen"
    assert screen["reference_time"] == SHOWN_AT
    assert screen["source"] == EpisodeType.message
    assert screen["saga"] == meeting_saga("meeting-1")
    assert screen["saga_previous_episode_uuid"] == "uuid-meeting-1:w00000"
    assert [row["window"] for row in rows if row["event"] == "memory_screen_ingested"] == [0]
    [finished] = [row for row in rows if row["event"] == "memory_ingest_finished"]
    assert (finished["screen_windows"], finished["screen_ingested"]) == (1, 1)


async def test_screen_without_title_or_text_is_counted_but_not_ingested() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result, _rows = await run(
        memory, [said(0, "Hello."), shown("screen-1", " ", text=" "), revenue_slide("screen-2")]
    )

    assert result.screen_windows == 2
    assert result.screen_uuids == {1: "uuid-meeting-1:s00001"}
    assert [episode["name"] for episode in memory.added] == ["meeting-1:w00000", "meeting-1:s00001"]


async def test_failed_screen_is_journaled_and_retried_on_resume() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    memory.errors[1] = RuntimeError("secret graph detail")
    entries = [said(0, "Look at the revenue slide please."), revenue_slide()]

    result, rows = await run(memory, entries)
    resumed, _rows = await run(memory, entries)

    assert (result.completed, result.screen_uuids) == (False, {})
    assert [row["event"] for row in rows if row["event"].startswith("memory_screen")] == [
        "memory_screen_failed"
    ]
    assert resumed.completed
    assert resumed.screen_uuids == {0: "uuid-meeting-1:s00000"}
    assert [episode["name"] for episode in memory.added] == ["meeting-1:w00000", "meeting-1:s00000"]


async def test_resume_skips_screens_already_in_the_saga() -> None:
    existing = [
        SimpleNamespace(uuid="old-0", name="meeting-1:w00000"),
        SimpleNamespace(uuid="old-screen", name="meeting-1:s00000"),
    ]
    memory = make_graphiti(episode_prefix="uuid-", existing=existing, edges=[])

    result, _rows = await run(
        memory, [said(0, "Hello."), revenue_slide(), shown("screen-2", "Roadmap")]
    )

    assert result.resumed_from == 1
    assert result.completed
    assert [episode["name"] for episode in memory.added] == ["meeting-1:s00001"]
    assert memory.added[0]["saga_previous_episode_uuid"] == "old-screen"


async def test_forget_meeting_cleans_screens(monkeypatch: pytest.MonkeyPatch) -> None:
    saga = meeting_saga("meeting-1")
    first = make_graphiti(episode_prefix="uuid-", edges=[])
    await run(first, [said(0, "Look at the revenue slide please."), revenue_slide()])
    names = [episode["name"] for episode in first.added]
    assert screen_episode_name("meeting-1", 0) in names
    uuids = [f"uuid-{name}" for name in names]
    second = make_graphiti(saga_episodes={saga: list(uuids)})
    deleted: list[str] = []

    class _Saga:
        def __init__(self, name: str) -> None:
            self.name = name

        async def delete(self, _driver: Any) -> None:
            deleted.append(self.name)

    async def get_by_group_ids(_driver: Any, _group_ids: list[str]) -> list[_Saga]:
        return [_Saga(saga)]

    monkeypatch.setattr(SagaNode, "get_by_group_ids", get_by_group_ids)

    removed = await forget_meeting(second, group_id="twin_group", meeting_id="meeting-1")

    assert removed == len(uuids)
    assert sorted(second.removed) == sorted(uuids)
    assert f"uuid-{screen_episode_name('meeting-1', 0)}" in second.removed
    assert deleted == [saga]


async def test_forget_meeting_reports_episode_that_survives_removal() -> None:
    memory = make_graphiti(existing=[SimpleNamespace(uuid="episode-stuck")])

    with pytest.raises(FakeYouError, match="survived removal") as error:
        await forget_meeting(memory, group_id="twin_group", meeting_id="meeting-1")

    assert error.value.code is ErrorCode.MEMORY_FORGET_INCOMPLETE


def test_a_screen_that_is_not_meeting_content_keeps_only_its_title() -> None:
    dashboard = shown(
        "screen-9",
        "Cartesia Usage",
        content="dashboard",
        key_points=("1,105 credits used",),
        figures=("1,105 credits",),
        visuals=("usage chart",),
        text="Receipt number 1450-6289, amount paid Rp899,900.00.",
    )

    body = screen_episode_body(dashboard)

    assert body == "Shared screen titled 'Cartesia Usage' shown by Rina (dashboard)."
