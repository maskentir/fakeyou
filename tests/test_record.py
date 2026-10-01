import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.record import (
    MeetingRecord,
    ToolResultEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
    meeting_record_path,
)

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def transcript(index: int, text: str = "Hello.", **overrides) -> TranscriptEvent:
    fields = {
        "id": f"event-{index}",
        "timestamp": f"2026-09-30T10:00:0{index}+00:00",
        "source": "meeting_audio",
        "text": text,
        "finalized": True,
    }
    fields.update(overrides)
    return TranscriptEvent(**fields)


def open_record(tmp_path: Path, **overrides) -> MeetingRecord:
    return MeetingRecord(meeting_record_path(tmp_path, "meeting-1"), now=lambda: NOW, **overrides)


def test_record_path_is_one_directory_per_meeting(tmp_path: Path) -> None:
    assert meeting_record_path(tmp_path, "meeting-1") == tmp_path / "meeting-1" / "record.jsonl"


def test_record_roundtrips_transcripts_twin_speech_and_tool_results(tmp_path: Path) -> None:
    record = open_record(tmp_path)

    record.add_transcript(transcript(0, "When is the Orchid demo?", participant="Speaker A"))
    record.add_twin_speech("The demo is Friday at two in the afternoon.")
    record.add_tool_result(
        "recall_meeting_context",
        "From owner note on 2026-09-29 08:00 UTC: The demo is on Friday",
        812.5,
    )
    record.close()

    assert load_meeting_record(meeting_record_path(tmp_path, "meeting-1")) == [
        TranscriptEntry(
            at=NOW,
            id="event-0",
            spoken_at="2026-09-30T10:00:00+00:00",
            participant="Speaker A",
            source="meeting_audio",
            text="When is the Orchid demo?",
        ),
        TwinSpeechEntry(at=NOW, text="The demo is Friday at two in the afternoon."),
        ToolResultEntry(
            at=NOW,
            tool="recall_meeting_context",
            output="From owner note on 2026-09-29 08:00 UTC: The demo is on Friday",
            duration_ms=812.5,
        ),
    ]


def test_record_is_append_only_json_lines(tmp_path: Path) -> None:
    first = open_record(tmp_path)
    first.add_transcript(transcript(0))
    first.close()
    second = open_record(tmp_path)
    second.add_twin_speech("Okay.")
    second.close()

    lines = meeting_record_path(tmp_path, "meeting-1").read_text(encoding="utf-8").splitlines()

    assert [json.loads(line)["kind"] for line in lines] == ["transcript", "twin_speech"]


def test_record_keeps_only_new_final_nonblank_transcripts(tmp_path: Path) -> None:
    record = open_record(tmp_path)

    record.add_transcript(transcript(0, "First."))
    record.add_transcript(transcript(0, "First."))
    record.add_transcript(transcript(1, "provisional", finalized=False))
    record.add_transcript(transcript(2, "   "))
    record.add_twin_speech("  ")
    record.close()

    entries = load_meeting_record(meeting_record_path(tmp_path, "meeting-1"))
    assert [entry.id for entry in entries if isinstance(entry, TranscriptEntry)] == ["event-0"]
    assert len(entries) == 1


def test_record_write_failure_is_reported_once_without_raising(journal, tmp_path: Path) -> None:
    record = open_record(tmp_path, faults=FaultReporter(journal=journal))
    record.close()

    record.add_transcript(transcript(0))
    record.add_twin_speech("Okay.")

    faults = journal.events()
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("meeting_record", "unknown")]


def test_read_meeting_record_rejects_unknown_entries(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, "meeting-1")
    path.parent.mkdir(parents=True)
    path.write_text('{"kind": "screenshot", "at": "2026-09-30T10:00:00+00:00"}\n', encoding="utf-8")

    with pytest.raises(ValueError):
        load_meeting_record(path)
