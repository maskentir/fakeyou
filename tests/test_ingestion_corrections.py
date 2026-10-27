import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
from graphiti_core.nodes import EpisodeType

from fakeyou.catalog.errors import ErrorCode
from fakeyou.diarization.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.diarization.diarize_correction import write_correction_bundle
from fakeyou.memory.ingestion import (
    CorrectionResult,
    IngestScope,
    MeetingScope,
    correction_episode_name,
    ingest_corrections,
    ingest_meeting,
)
from fakeyou.memory.memory import meeting_saga
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.record import TranscriptEntry, load_meeting_record
from tests._error_support import message_pattern
from tests._ingestion_support import CORRECTED_AT, START, correction_record, run, said
from tests._memory_fakes import make_graphiti


async def test_ingest_after_correction_skips_corrected_episodes_when_resuming(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    entries = load_meeting_record(record_path)
    await ingest_meeting(
        memory,
        entries,
        IngestScope(group_id="twin_group", meeting_id="meeting-1", twin_name="Rani Twin"),
    )
    await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    result = await ingest_meeting(
        memory,
        entries,
        IngestScope(group_id="twin_group", meeting_id="meeting-1", twin_name="Rani Twin"),
    )

    assert (result.resumed_from, result.ingested, result.completed) == (3, 0, True)
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
        "meeting-1:w00002",
        "meeting-1:w00001:corrected",
    ]


async def test_ingest_resumes_past_corrected_episodes_and_chains_from_the_latest() -> None:
    entries = [
        said(
            index * 61,
            f"Sentence {index} covers the Thursday launch plan with the rollout checklist "
            "and the client demo schedule for all regions this week.",
        )
        for index in range(3)
    ]
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[SimpleNamespace(fact="fact")],
        existing=[
            SimpleNamespace(uuid="old-0", name="meeting-1:w00000"),
            SimpleNamespace(uuid="old-fix", name="meeting-1:w00000:corrected"),
        ],
    )

    result, _rows = await run(memory, entries)

    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00001",
        "meeting-1:w00002",
    ]
    assert memory.added[0]["saga_previous_episode_uuid"] == "old-fix"
    assert (result.resumed_from, result.ingested, result.completed) == (1, 2, True)


async def test_ingest_corrections_reingests_only_changed_windows(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    await ingest_meeting(
        memory,
        load_meeting_record(record_path),
        IngestScope(group_id="twin_group", meeting_id="meeting-1", twin_name="Rani Twin"),
    )

    before = datetime.now(UTC)
    lines: list[str] = []
    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
        diagnostics=Diagnostics(journal=Journal("meeting-1", emit=lines.append)),
    )
    after = datetime.now(UTC)

    assert isinstance(result, CorrectionResult)
    assert result.changed_windows == 1
    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert result.completed is True
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
        "meeting-1:w00002",
        correction_episode_name("meeting-1", 1),
    ]
    episode = memory.added[-1]
    assert episode["episode_body"] == (
        "Harun: Budi is the PIC for the Thursday launch and owns the rollout checklist "
        "end to end without further review from the client team."
    )
    assert episode["source"] == EpisodeType.message
    assert episode["source_description"] == "Google Meet meeting-1 transcript (corrected)"
    assert before <= episode["reference_time"] <= after
    assert episode["reference_time"] != START + timedelta(seconds=61)
    assert episode["group_id"] == "twin_group"
    assert episode["saga"] == meeting_saga("meeting-1")
    assert episode["saga_previous_episode_uuid"] == "uuid-meeting-1:w00002"
    rows = [json.loads(line) for line in lines]
    assert [row["event"] for row in rows if row["event"].startswith("memory_correction")] == [
        "memory_correction_ingested",
        "memory_correction_finished",
    ]
    ingested = next(row for row in rows if row["event"] == "memory_correction_ingested")
    [finished] = [row for row in rows if row["event"] == "memory_correction_finished"]
    assert ingested["window"] == 1
    assert (finished["changed"], finished["ingested"], finished["completed"]) == (1, 1, True)


async def test_ingest_corrections_keeps_window_map_and_logs_corrections(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="meeting-1",
            group_id="twin_group",
            status="completed",
            windows=3,
            ingested=3,
            episode_uuids={
                0: "uuid-meeting-1:w00000",
                1: "uuid-meeting-1:w00001",
                2: "uuid-meeting-1:w00002",
            },
        ),
    )
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.episode_uuids == {
        0: "uuid-meeting-1:w00000",
        1: "uuid-meeting-1:w00001",
        2: "uuid-meeting-1:w00002",
    }
    assert status.corrected_at == CORRECTED_AT.isoformat()
    assert status.correction_episodes == ["uuid-meeting-1:w00001:corrected"]


async def test_ingest_corrections_second_run_ingests_zero_episodes(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    added = len(memory.added)
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert first.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert second.changed_windows == 1
    assert second.episodes == ()
    assert second.completed is True
    assert len(memory.added) == added


async def test_ingest_corrections_rejects_stale_sidecar(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    lines = record_path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = said(0, "Rewritten inside the corrected prefix.").model_dump_json() + "\n"
    record_path.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(DiarizeError, match=message_pattern(ErrorCode.DIARIZE_STALE)) as error:
        await ingest_corrections(
            memory,
            record_path,
            MeetingScope("twin_group", "meeting-1"),
        )

    assert error.value.code is ErrorCode.DIARIZE_STALE
    assert memory.added == []
    assert not status_path(record_path).exists()


async def test_ingest_corrections_accepts_record_growth_after_correction(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(said(200, "Late arrival.").model_dump_json() + "\n")

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert result.completed is True


async def test_ingest_corrections_without_sidecar_ingests_nothing(tmp_path: Path) -> None:
    record_path = tmp_path / "meeting-1" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(said(0, "Hello.").model_dump_json() + "\n", encoding="utf-8")
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result == CorrectionResult(changed_windows=0, episodes=(), completed=True)
    assert memory.added == []


def correction_status(record_path: Path) -> None:
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="meeting-1",
            group_id="twin_group",
            status="completed",
            windows=3,
            ingested=3,
            episode_uuids={
                0: "uuid-meeting-1:w00000",
                1: "uuid-meeting-1:w00001",
                2: "uuid-meeting-1:w00002",
            },
        ),
    )


async def test_ingest_corrections_invalidates_original_edges_of_changed_windows(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[],
        episode_edges={
            "uuid-meeting-1:w00000": [
                SimpleNamespace(uuid="edge-stable-0", invalid_at=None, expired_at=None)
            ],
            "uuid-meeting-1:w00001": [
                SimpleNamespace(uuid="edge-changed-a", invalid_at=None, expired_at=None),
                SimpleNamespace(uuid="edge-changed-b", invalid_at=None, expired_at=None),
                SimpleNamespace(
                    uuid="edge-already-invalid", invalid_at=CORRECTED_AT, expired_at=None
                ),
            ],
            "uuid-meeting-1:w00002": [
                SimpleNamespace(uuid="edge-stable-2", invalid_at=None, expired_at=None)
            ],
        },
    )

    lines: list[str] = []
    before = datetime.now(UTC)
    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
        diagnostics=Diagnostics(journal=Journal("meeting-1", emit=lines.append)),
    )
    after = datetime.now(UTC)

    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert result.completed is True
    assert memory.episode_lookups == [["uuid-meeting-1:w00001"]]
    assert sorted(call["uuid"] for call in memory.executed) == [
        "edge-changed-a",
        "edge-changed-b",
    ]
    for call in memory.executed:
        assert "RELATES_TO" in call["query"]
        assert "invalid_at" in call["query"]
        assert "expired_at" in call["query"]
        assert call["group_id"] == "twin_group"
        assert before <= call["at"] <= after
    rows = [json.loads(line) for line in lines]
    ingested = next(row for row in rows if row["event"] == "memory_correction_ingested")
    assert ingested["invalidated"] == 2


async def test_ingest_corrections_without_window_map_skips_invalidation(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    journal = Journal("meeting-1", echo=False)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
        diagnostics=Diagnostics(faults=faults),
    )

    assert result.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert result.completed is True
    assert memory.episode_lookups == []
    assert memory.executed == []
    assert faults.total() == 1


async def test_ingest_corrections_aborts_when_invalidation_fails(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[],
        episode_edges={
            "uuid-meeting-1:w00001": [
                SimpleNamespace(uuid="edge-changed-a", invalid_at=None, expired_at=None)
            ]
        },
    )

    async def failing(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise RuntimeError("neo4j is down")

    memory.driver.execute_query.side_effect = failing
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
        diagnostics=Diagnostics(faults=faults, journal=journal),
    )

    assert result.completed is False
    assert result.episodes == ()
    assert faults.total() == 1
    rows = [json.loads(line) for line in lines]
    assert "memory_correction_failed" in [row["event"] for row in rows]
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.corrected_at is None


async def test_ingest_corrections_invalidation_failure_fault_carries_orphan_uuid(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[],
        episode_edges={
            "uuid-meeting-1:w00001": [
                SimpleNamespace(uuid="edge-changed-a", invalid_at=None, expired_at=None)
            ]
        },
    )

    async def failing(*args: Any, **_kwargs: Any) -> NoReturn:
        del args
        raise RuntimeError("neo4j is down")

    memory.driver.execute_query.side_effect = failing
    journal = Journal("meeting-1", echo=False)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
        diagnostics=Diagnostics(faults=faults, journal=journal),
    )

    assert result.completed is False
    assert result.episodes == ()
    assert [episode["name"] for episode in memory.added] == [
        correction_episode_name("meeting-1", 1)
    ]
    assert faults.total() == 1
    [summary] = faults.summaries()
    assert summary.detail is not None
    assert "uuid-meeting-1:w00001:corrected" in summary.detail
    assert "window 1" in summary.detail


def correction_record_three(tmp_path: Path) -> Path:
    lines = [
        (
            0,
            "The budget is approved for the Thursday launch, including the rollout checklist, "
            "the client demo schedule, and the release notes for all regions.",
            1000.0,
        ),
        (
            61,
            "Guest is the PIC for the Thursday launch and owns the rollout checklist "
            "end to end without further review from the client team.",
            70000.0,
        ),
        (
            122,
            "We ship on Friday after the client demo, and the changelog plus the release notes "
            "will be ready on time for all regions this week.",
            130000.0,
        ),
    ]
    entries = [
        TranscriptEntry(
            at=START + timedelta(seconds=seconds),
            seq=index + 1,
            id=f"m{index}",
            spoken_at=(START + timedelta(seconds=seconds)).isoformat(),
            source="meeting_audio",
            text=text,
            participant_id="unknown",
            speaker_evidence_id=None,
            capture_ms=capture,
        )
        for index, (seconds, text, capture) in enumerate(lines)
    ]
    record_path = tmp_path / "meeting-1" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        "".join(entry.model_dump_json() + "\n" for entry in entries), encoding="utf-8"
    )
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [
            DiarizedTurn(start_ms=0, end_ms=2000, speaker="Host"),
            DiarizedTurn(start_ms=69000, end_ms=71000, speaker="Guest"),
            DiarizedTurn(start_ms=129000, end_ms=131000, speaker="Twin"),
        ],
        clock=lambda: CORRECTED_AT,
    )
    return record_path


async def test_ingest_corrections_retry_after_midrun_failure_writes_no_duplicates(
    tmp_path: Path,
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    memory.errors[1] = RuntimeError("boom")

    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    assert first.completed is False
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert second.changed_windows == 3
    assert second.completed is True
    names = [episode["name"] for episode in memory.added]
    assert sorted(names) == sorted(
        [correction_episode_name("meeting-1", index) for index in range(3)]
    )
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.corrected_at == CORRECTED_AT.isoformat()


async def test_ingest_corrections_partial_map_survives_failure_and_retry_skips_it(
    tmp_path: Path,
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    memory.errors[1] = RuntimeError("boom")

    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert first.completed is False
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.corrected_at is None
    assert status.correction_windows == {0: "uuid-meeting-1:w00000:corrected"}
    added_before_retry = len(memory.added)
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert second.completed is True
    names = [episode["name"] for episode in memory.added[added_before_retry:]]
    assert names == [
        correction_episode_name("meeting-1", 1),
        correction_episode_name("meeting-1", 2),
    ]


async def test_ingest_corrections_new_run_resets_window_map(tmp_path: Path) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert first.completed is True
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.correction_run == CORRECTED_AT.isoformat()
    assert sorted(status.correction_windows) == [0, 1, 2]
    rotated = CORRECTED_AT + timedelta(hours=1)
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [DiarizedTurn(start_ms=69000, end_ms=71000, speaker="Guest")],
        clock=lambda: rotated,
    )
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert second.completed is True
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.correction_run == rotated.isoformat()
    assert status.correction_windows == {1: "uuid-meeting-1:w00001:corrected"}


async def test_ingest_corrections_status_write_failure_reports_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    calls = 0
    real_write_status = write_status

    def failing_write(path: Path, status: IngestionStatus) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("status store is down")
        real_write_status(path, status)

    monkeypatch.setattr("fakeyou.memory.ingestion.write_status", failing_write)

    result = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert result.completed is False
    assert calls == 2
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.corrected_at is None
    assert status.correction_windows == {0: "uuid-meeting-1:w00000:corrected"}


async def test_ingest_corrections_rotation_retry_recovers_same_run_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="episode-", edges=[])
    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    assert first.completed is True
    rotated = CORRECTED_AT + timedelta(hours=1)
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [
            DiarizedTurn(start_ms=0, end_ms=2000, speaker="Host"),
            DiarizedTurn(start_ms=69000, end_ms=71000, speaker="Guest"),
            DiarizedTurn(start_ms=129000, end_ms=131000, speaker="Twin"),
        ],
        clock=lambda: rotated,
    )
    calls = 0
    real_write_status = write_status

    def flaky_write(path: Path, status: IngestionStatus) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("status store is down")
        real_write_status(path, status)

    monkeypatch.setattr("fakeyou.memory.ingestion.write_status", flaky_write)
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    assert second.completed is False
    third = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert third.completed is True
    names = [episode["name"] for episode in memory.added]
    assert names.count(correction_episode_name("meeting-1", 1)) == 2


async def test_ingest_corrections_rotation_reingests_overlapping_windows_with_new_labels(
    tmp_path: Path,
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="episode-", edges=[])
    memory.errors[1] = RuntimeError("boom")
    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    assert first.completed is False
    rotated = CORRECTED_AT + timedelta(hours=1)
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [
            DiarizedTurn(start_ms=0, end_ms=2000, speaker="Guest"),
            DiarizedTurn(start_ms=69000, end_ms=71000, speaker="Host"),
            DiarizedTurn(start_ms=129000, end_ms=131000, speaker="Twin"),
        ],
        clock=lambda: rotated,
    )
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert second.completed is True
    bodies = [
        episode["episode_body"]
        for episode in memory.added
        if episode["name"] == correction_episode_name("meeting-1", 0)
    ]
    assert bodies == [
        "Host: The budget is approved for the Thursday launch, including the rollout "
        "checklist, the client demo schedule, and the release notes for all regions.",
        "Guest: The budget is approved for the Thursday launch, including the rollout "
        "checklist, the client demo schedule, and the release notes for all regions.",
    ]


async def test_ingest_corrections_completion_unions_prior_attempt_episodes(
    tmp_path: Path,
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    memory.errors[2] = RuntimeError("boom")
    first = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )
    assert first.completed is False
    second = await ingest_corrections(
        memory,
        record_path,
        MeetingScope("twin_group", "meeting-1"),
    )

    assert second.completed is True
    status = read_status(status_path(record_path))
    assert status is not None
    assert sorted(status.correction_episodes) == sorted(status.correction_windows.values())
    assert len(status.correction_episodes) == 3


async def test_correction_adoption_issues_one_bounded_saga_retrieval(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    await ingest_meeting(
        memory,
        load_meeting_record(record_path),
        IngestScope(group_id="twin_group", meeting_id="meeting-1", twin_name="Rani Twin"),
    )
    memory.retrieved.clear()

    result = await ingest_corrections(memory, record_path, MeetingScope("twin_group", "meeting-1"))

    assert result.completed is True
    saga_retrievals = [call for call in memory.retrieved if call["last_n"] > 1]
    assert [call["last_n"] for call in saga_retrievals] == [2 * 3]
