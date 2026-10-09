import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from graphiti_core.nodes import EpisodeType

from fakeyou.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.diarize_correction import write_correction_bundle
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import (
    TWIN_SPEAKER,
    UNIDENTIFIED_PARTICIPANT,
    CorrectionResult,
    correction_episode_name,
    episode_name,
    extraction_instructions,
    ingest_corrections,
    ingest_meeting,
    meeting_windows,
)
from fakeyou.ingestion_status import IngestionStatus, read_status, status_path, write_status
from fakeyou.journal import Journal
from fakeyou.memory import meeting_saga
from fakeyou.record import (
    ToolResultEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
)
from tests.fakes import make_graphiti

START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
CORRECTED_AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def said(seconds: float, text: str, participant_id: str = "unknown") -> TranscriptEntry:
    return TranscriptEntry(
        at=START + timedelta(seconds=seconds),
        id=f"t{seconds}",
        spoken_at=(START + timedelta(seconds=seconds)).isoformat(),
        source="meeting_audio",
        text=text,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def twin(seconds: float, text: str) -> TwinSpeechEntry:
    return TwinSpeechEntry(at=START + timedelta(seconds=seconds), text=text)


async def run(memory: MagicMock, entries: list, **overrides):
    lines: list[str] = []
    values = {
        "group_id": "twin_group",
        "meeting_id": "meeting-1",
        "twin_name": "Rani Twin",
        "vocabulary": ("Orchid", "Budi"),
        "journal": Journal("meeting-1", emit=lines.append),
    }
    values["faults"] = FaultReporter(journal=values["journal"])
    values.update(overrides)
    result = await ingest_meeting(memory, entries, **values)
    return result, [json.loads(line) for line in lines]


def test_windows_mix_speakers_in_record_order_and_skip_tool_results() -> None:
    entries = [
        twin(0, "How about moving the demo to Thursday?"),
        said(2, "Okay,\n agreed."),
        ToolResultEntry(at=START, tool="recall_meeting_context", output="x", duration_ms=1),
        said(3, "The budget is fine.", participant_id="Speaker A"),
    ]

    [window] = meeting_windows(entries)

    assert [(utterance.speaker, utterance.text) for utterance in window] == [
        (TWIN_SPEAKER, "How about moving the demo to Thursday?"),
        (UNIDENTIFIED_PARTICIPANT, "Okay, agreed."),
        ("Speaker A", "The budget is fine."),
    ]


def test_windows_split_by_utterance_count_and_age() -> None:
    by_count = meeting_windows([said(index, f"Sentence {index}.") for index in range(45)])
    by_age = meeting_windows([said(0, "Start."), said(59, "Still going."), said(61, "New window.")])

    assert [len(window) for window in by_count] == [20, 20, 5]
    assert [len(window) for window in by_age] == [2, 1]


def test_windows_are_deterministic() -> None:
    entries = [said(index * 7, f"Sentence {index}.") for index in range(30)]

    assert meeting_windows(entries) == meeting_windows(list(entries))


def test_extraction_instructions_require_attribution_and_vocabulary() -> None:
    instructions = extraction_instructions("Rani Twin", ("Orchid", "Budi"))

    for phrase in (f"'{TWIN_SPEAKER}'", "'Rani Twin'", f"'{UNIDENTIFIED_PARTICIPANT}'"):
        assert phrase in instructions
    assert "never the owner" in instructions
    assert "speaker as the subject" in instructions
    assert "someone asking" in instructions
    assert "Orchid, Budi" in instructions
    assert "sounds similar" in instructions
    assert "language the speakers used" in instructions


def test_unidentified_speaker_label_leaves_owner_identity_open() -> None:
    assert UNIDENTIFIED_PARTICIPANT == "Speaker with unverified identity"
    instructions = extraction_instructions("Rani Twin", ())

    assert "may be the owner or any named person" in instructions
    assert "never merge it" in instructions


def test_extraction_instructions_without_vocabulary_do_not_list_terms() -> None:
    assert "Known names" not in extraction_instructions("Rani Twin", ())


async def test_ingest_writes_one_message_episode_per_window_in_the_meeting_saga() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        twin(0, "Move the demo to Thursday?"),
        said(1, "Okay, agreed."),
        said(70, "Budi is the PIC."),
    ]

    result, rows = await run(memory, entries)

    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
    ]
    first = memory.added[0]
    assert first["episode_body"] == (
        f"{TWIN_SPEAKER}: Move the demo to Thursday?\n{UNIDENTIFIED_PARTICIPANT}: Okay, agreed."
    )
    assert first["source"] == EpisodeType.message
    assert first["source_description"] == "Google Meet meeting-1 transcript"
    assert first["reference_time"] == START
    assert first["group_id"] == "twin_group"
    assert first["saga"] == meeting_saga("meeting-1")
    assert "Orchid, Budi" in first["custom_extraction_instructions"]
    assert [episode["saga_previous_episode_uuid"] for episode in memory.added] == [
        None,
        "uuid-meeting-1:w00000",
    ]
    assert (result.windows, result.resumed_from, result.ingested, result.completed) == (
        2,
        0,
        2,
        True,
    )
    assert [
        (row["event"], row["facts"]) for row in rows if row["event"].startswith("memory_episode")
    ] == [("memory_episode_ingested", 1), ("memory_episode_ingested", 1)]
    [finished] = [row for row in rows if row["event"] == "memory_ingest_finished"]
    assert (finished["windows"], finished["ingested"], finished["completed"]) == (2, 2, True)


async def test_ingest_uses_spoken_time_as_the_window_source_time() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    spoken = datetime(2026, 9, 30, 8, 58, 30, tzinfo=UTC)
    entries = [
        TranscriptEntry(
            at=START,
            id="late-record",
            spoken_at=spoken.isoformat(),
            source="meeting_audio",
            text="The launch stays on Friday.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        )
    ]

    result, _rows = await run(memory, entries)

    assert result.completed
    [episode] = memory.added
    assert episode["reference_time"] == spoken


async def test_ingest_falls_back_to_record_time_for_invalid_spoken_time() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        TranscriptEntry(
            at=START,
            id="bad-clock",
            spoken_at="not-a-timestamp",
            source="meeting_audio",
            text="The launch stays on Friday.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        ),
        TranscriptEntry(
            at=START,
            id="naive-clock",
            spoken_at="2026-09-30T09:00:00",
            source="meeting_audio",
            text="The venue stays Hall A.",
            participant_id="Speaker A",
            speaker_evidence_id=None,
        ),
    ]

    result, _rows = await run(memory, entries)

    assert result.completed
    [episode] = memory.added
    assert episode["reference_time"] == START


async def test_ingest_resumes_after_the_last_saga_episode_and_is_idempotent() -> None:
    entries = [said(index * 61, f"Sentence {index}.") for index in range(3)]
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[SimpleNamespace(fact="fact")],
        existing=[SimpleNamespace(uuid="old-0", name="meeting-1:w00000")],
    )

    result, _rows = await run(memory, entries)
    again, _rows = await run(memory, entries)

    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00001",
        "meeting-1:w00002",
    ]
    assert memory.added[0]["saga_previous_episode_uuid"] == "old-0"
    assert (result.resumed_from, result.ingested) == (1, 2)
    assert (again.resumed_from, again.ingested, again.completed) == (3, 0, True)
    assert memory.retrieved[0] == {
        "last_n": 1,
        "group_ids": ["twin_group"],
        "saga": meeting_saga("meeting-1"),
    }


async def test_ingest_stops_at_a_failed_window_so_a_rerun_continues_there() -> None:
    entries = [said(index * 61, f"Sentence {index}.") for index in range(3)]
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    memory.errors[1] = RuntimeError("secret graph detail")

    result, rows = await run(memory, entries)
    resumed, _rows = await run(memory, entries)

    assert (result.ingested, result.completed) == (1, False)
    assert [(row["source"], row["kind"]) for row in rows if row["event"] == "fault_recorded"] == [
        ("memory_ingest", "unknown")
    ]
    assert "secret graph detail" not in json.dumps(rows)
    assert (resumed.resumed_from, resumed.ingested, resumed.completed) == (1, 2, True)
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
        "meeting-1:w00002",
    ]


async def test_ingest_times_out_a_hung_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    memory.gate = asyncio.Event()

    result, rows = await run(memory, [said(0, "Stuck.")], episode_timeout_seconds=0.01)

    assert (result.ingested, result.completed) == (0, False)
    assert [row["kind"] for row in rows if row["event"] == "fault_recorded"] == ["timeout"]


async def test_ingest_stops_between_windows_when_requested() -> None:
    stop = asyncio.Event()
    stop.set()
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])

    result, _rows = await run(memory, [said(0, "One.")], stop=stop)

    assert (result.ingested, result.completed) == (0, False)
    assert memory.added == []


async def test_ingest_rejects_an_invalid_episode_name_in_the_saga() -> None:
    memory = make_graphiti(
        episode_prefix="uuid-",
        edges=[SimpleNamespace(fact="fact")],
        existing=[SimpleNamespace(uuid="invalid-episode", name="not-a-window")],
    )

    with pytest.raises(ValueError, match="invalid episode name"):
        await run(memory, [said(0, "Hello.")])


async def test_ingest_after_correction_skips_corrected_episodes_when_resuming(
    tmp_path: Path,
) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    entries = load_meeting_record(record_path)
    await ingest_meeting(
        memory,
        entries,
        group_id="twin_group",
        meeting_id="meeting-1",
        twin_name="Rani Twin",
    )
    await ingest_corrections(memory, record_path, group_id="twin_group", meeting_id="meeting-1")

    result = await ingest_meeting(
        memory,
        entries,
        group_id="twin_group",
        meeting_id="meeting-1",
        twin_name="Rani Twin",
    )

    assert (result.resumed_from, result.ingested, result.completed) == (3, 0, True)
    assert [episode["name"] for episode in memory.added] == [
        "meeting-1:w00000",
        "meeting-1:w00001",
        "meeting-1:w00002",
        "meeting-1:w00001:corrected",
    ]


async def test_ingest_resumes_past_corrected_episodes_and_chains_from_the_latest() -> None:
    entries = [said(index * 61, f"Sentence {index}.") for index in range(3)]
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


async def test_ingest_of_an_empty_record_is_complete() -> None:
    result, _rows = await run(
        make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")]), []
    )

    assert (result.windows, result.ingested, result.completed) == (0, 0, True)


def test_episode_names_sort_by_window_index() -> None:
    assert episode_name("meeting-1", 7) == "meeting-1:w00007"


async def test_ingest_result_carries_episode_uuids_per_window() -> None:
    memory = make_graphiti(episode_prefix="uuid-", edges=[SimpleNamespace(fact="fact")])
    entries = [
        twin(0, "Move the demo to Thursday?"),
        said(1, "Okay, agreed."),
        said(70, "Budi is the PIC."),
    ]

    result, _rows = await run(memory, entries)

    assert result.episode_uuids == {
        0: "uuid-meeting-1:w00000",
        1: "uuid-meeting-1:w00001",
    }


def correction_record(tmp_path: Path) -> Path:
    lines = [
        (0, "The budget is approved.", 1000.0),
        (61, "Budi is the PIC.", 70000.0),
        (122, "We ship on Friday.", 130000.0),
    ]
    entries = [
        TranscriptEntry(
            at=START + timedelta(seconds=seconds),
            id=f"c{index}",
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
        [DiarizedTurn(start_ms=69000, end_ms=71000, speaker="Harun")],
        clock=lambda: CORRECTED_AT,
    )
    return record_path


async def test_ingest_corrections_reingests_only_changed_windows(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    await ingest_meeting(
        memory,
        load_meeting_record(record_path),
        group_id="twin_group",
        meeting_id="meeting-1",
        twin_name="Rani Twin",
    )

    before = datetime.now(UTC)
    lines: list[str] = []
    result = await ingest_corrections(
        memory,
        record_path,
        group_id="twin_group",
        meeting_id="meeting-1",
        journal=Journal("meeting-1", emit=lines.append),
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
    assert episode["episode_body"] == "Harun: Budi is the PIC."
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )
    added = len(memory.added)
    second = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )

    assert first.episodes == ("uuid-meeting-1:w00001:corrected",)
    assert second.changed_windows == 1
    assert second.episodes == ()
    assert second.completed is True
    assert len(memory.added) == added


async def test_ingest_corrections_rejects_stale_sidecar(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(said(200, "Late arrival.").model_dump_json() + "\n")

    with pytest.raises(DiarizeError):
        await ingest_corrections(memory, record_path, group_id="twin_group", meeting_id="meeting-1")

    assert memory.added == []
    assert not status_path(record_path).exists()


async def test_ingest_corrections_without_sidecar_ingests_nothing(tmp_path: Path) -> None:
    record_path = tmp_path / "meeting-1" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(said(0, "Hello.").model_dump_json() + "\n", encoding="utf-8")
    memory = make_graphiti(episode_prefix="uuid-", edges=[])

    result = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        group_id="twin_group",
        meeting_id="meeting-1",
        journal=Journal("meeting-1", emit=lines.append),
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
        assert "invalid_at" in call["query"] and "expired_at" in call["query"]
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
    journal = Journal("meeting-1", stdout=False)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1", faults=faults
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

    async def failing(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    memory.driver.execute_query.side_effect = failing
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory,
        record_path,
        group_id="twin_group",
        meeting_id="meeting-1",
        journal=journal,
        faults=faults,
    )

    assert result.completed is False
    assert result.episodes == ()
    assert faults.total() == 1
    rows = [json.loads(line) for line in lines]
    assert "memory_correction_failed" in [row["event"] for row in rows]
    assert read_status(status_path(record_path)) is not None
    assert read_status(status_path(record_path)).corrected_at is None


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

    async def failing(*args, **kwargs):
        raise RuntimeError("neo4j is down")

    memory.driver.execute_query.side_effect = failing
    journal = Journal("meeting-1", stdout=False)
    faults = FaultReporter(journal=journal)

    result = await ingest_corrections(
        memory,
        record_path,
        group_id="twin_group",
        meeting_id="meeting-1",
        journal=journal,
        faults=faults,
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
        (0, "The budget is approved.", 1000.0),
        (61, "Guest is the PIC.", 70000.0),
        (122, "We ship on Friday.", 130000.0),
    ]
    entries = [
        TranscriptEntry(
            at=START + timedelta(seconds=seconds),
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )
    assert first.completed is False
    second = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )

    assert first.completed is False
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.corrected_at is None
    assert status.correction_windows == {0: "uuid-meeting-1:w00000:corrected"}
    added_before_retry = len(memory.added)
    second = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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

    monkeypatch.setattr("fakeyou.ingestion.write_status", failing_write)

    result = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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

    monkeypatch.setattr("fakeyou.ingestion.write_status", flaky_write)
    second = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )
    assert second.completed is False
    third = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
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
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )

    assert second.completed is True
    bodies = [
        episode["episode_body"]
        for episode in memory.added
        if episode["name"] == correction_episode_name("meeting-1", 0)
    ]
    assert bodies == ["Host: The budget is approved.", "Guest: The budget is approved."]


async def test_ingest_corrections_completion_unions_prior_attempt_episodes(
    tmp_path: Path,
) -> None:
    record_path = correction_record_three(tmp_path)
    correction_status(record_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    memory.errors[2] = RuntimeError("boom")
    first = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )
    assert first.completed is False
    second = await ingest_corrections(
        memory, record_path, group_id="twin_group", meeting_id="meeting-1"
    )

    assert second.completed is True
    status = read_status(status_path(record_path))
    assert status is not None
    assert sorted(status.correction_episodes) == sorted(status.correction_windows.values())
    assert len(status.correction_episodes) == 3


def test_ingestion_status_old_schema_json_defaults_new_correction_fields() -> None:
    payload = {
        "meeting_id": "meeting-1",
        "group_id": "twin_group",
        "status": "completed",
        "windows": 3,
        "resumed_from": 0,
        "ingested": 3,
        "record_sha256": None,
        "updated_at": CORRECTED_AT.isoformat(),
        "reason": None,
        "episode_uuids": {"0": "uuid-a"},
        "corrected_at": CORRECTED_AT.isoformat(),
        "correction_episodes": ["uuid-fix"],
    }

    status = IngestionStatus.model_validate_json(json.dumps(payload))

    assert status.correction_windows == {}
    assert status.correction_run is None
