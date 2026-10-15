from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from graphiti_core.nodes import EpisodeType, SagaNode

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.memory import meeting_saga
from fakeyou.persistence.record import VisualEntry
from tests._ingestion_support import START, run, said
from tests._memory_fakes import make_graphiti


def visual_entry(visual_id: str, caption: str, captured: datetime, ref: str) -> VisualEntry:
    return VisualEntry(
        at=START,
        visual_id=visual_id,
        captured_at=captured,
        captured_page_ms=1727687100000.0,
        sha256="cd" * 32,
        caption=caption,
        frame_ref=ref,
    )


async def test_ingest_visual_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    captured = datetime(2026, 9, 30, 9, 5, tzinfo=UTC)
    ref = f"frame:{'cd' * 4}@{captured.isoformat()}"
    entries = [
        said(0, "Look at the revenue slide please."),
        visual_entry("frame-1", "Quarterly revenue grew twelve percent.", captured, ref),
    ]

    result, rows = await run(memory, entries)

    assert result.visual_windows == 1
    assert result.completed
    [visual] = [episode for episode in memory.added if episode["name"] == "meeting-1:v00000"]
    assert visual["episode_body"] == f"Shared screen {ref}: Quarterly revenue grew twelve percent."
    assert visual["source_description"] == "Google Meet meeting-1 visual"
    assert visual["reference_time"] == captured
    assert visual["source"] == EpisodeType.message
    assert visual["saga"] == meeting_saga("meeting-1")
    assert visual["saga_previous_episode_uuid"] == "uuid-meeting-1:w00000"
    assert "base64" not in visual["episode_body"]
    [finished] = [row for row in rows if row["event"] == "memory_ingest_finished"]
    assert (finished["visual_windows"], finished["visual_ingested"]) == (1, 1)


async def test_forget_meeting_cleans_visuals(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeyou.memory.ingestion import visual_episode_name
    from fakeyou.memory.memory import forget_meeting

    saga = meeting_saga("meeting-1")
    captured = datetime(2026, 9, 30, 9, 5, tzinfo=UTC)
    ref = f"frame:{'cd' * 4}@{captured.isoformat()}"
    first = make_graphiti(episode_prefix="uuid-", edges=[])
    await run(
        first,
        [
            said(0, "Look at the revenue slide please."),
            visual_entry("frame-1", "Quarterly revenue grew twelve percent.", captured, ref),
        ],
    )
    names = [episode["name"] for episode in first.added]
    assert visual_episode_name("meeting-1", 0) in names
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
    assert f"uuid-{visual_episode_name('meeting-1', 0)}" in second.removed
    assert deleted == [saga]


async def test_forget_meeting_reports_episode_that_survives_removal() -> None:
    from fakeyou.memory.memory import forget_meeting

    memory = make_graphiti(existing=[SimpleNamespace(uuid="episode-stuck")])

    with pytest.raises(FakeYouError, match="survived removal") as error:
        await forget_meeting(memory, group_id="twin_group", meeting_id="meeting-1")

    assert error.value.code is ErrorCode.MEMORY_FORGET_INCOMPLETE
