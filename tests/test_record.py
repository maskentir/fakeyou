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
    meeting_record_path,
    read_meeting_record,
)

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def transcript(index: int, text: str = "Halo.", **overrides) -> TranscriptEvent:
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

    record.add_transcript(transcript(0, "Kapan demo Orchid?", participant="Speaker A"))
    record.add_twin_speech("Demonya Jumat jam dua siang.")
    record.add_tool_result("recall_meeting_context", "Demo Jumat (source: owner note)", 812.5)
    record.close()

    assert read_meeting_record(meeting_record_path(tmp_path, "meeting-1")) == [
        TranscriptEntry(
            at=NOW,
            id="event-0",
            spoken_at="2026-09-30T10:00:00+00:00",
            participant="Speaker A",
            source="meeting_audio",
            text="Kapan demo Orchid?",
        ),
        TwinSpeechEntry(at=NOW, text="Demonya Jumat jam dua siang."),
        ToolResultEntry(
            at=NOW,
            tool="recall_meeting_context",
            output="Demo Jumat (source: owner note)",
            duration_ms=812.5,
        ),
    ]


def test_record_is_append_only_json_lines(tmp_path: Path) -> None:
    first = open_record(tmp_path)
    first.add_transcript(transcript(0))
    first.close()
    second = open_record(tmp_path)
    second.add_twin_speech("Oke.")
    second.close()

    lines = meeting_record_path(tmp_path, "meeting-1").read_text(encoding="utf-8").splitlines()

    assert [json.loads(line)["kind"] for line in lines] == ["transcript", "twin_speech"]


def test_record_keeps_only_new_final_nonblank_transcripts(tmp_path: Path) -> None:
    record = open_record(tmp_path)

    record.add_transcript(transcript(0, "Pertama."))
    record.add_transcript(transcript(0, "Pertama."))
    record.add_transcript(transcript(1, "sementara", finalized=False))
    record.add_transcript(transcript(2, "   "))
    record.add_twin_speech("  ")
    record.close()

    entries = read_meeting_record(meeting_record_path(tmp_path, "meeting-1"))
    assert [entry.id for entry in entries if isinstance(entry, TranscriptEntry)] == ["event-0"]
    assert len(entries) == 1


def test_record_write_failure_is_reported_once_without_raising(tmp_path: Path) -> None:
    lines: list[str] = []
    record = open_record(tmp_path, faults=FaultReporter(meeting_id="meeting-1", log=lines.append))
    record.close()

    record.add_transcript(transcript(0))
    record.add_twin_speech("Oke.")

    faults = [json.loads(line) for line in lines]
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("meeting_record", "unknown")]


def test_read_meeting_record_rejects_unknown_entries(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, "meeting-1")
    path.parent.mkdir(parents=True)
    path.write_text('{"kind": "screenshot", "at": "2026-09-30T10:00:00+00:00"}\n', encoding="utf-8")

    with pytest.raises(ValueError):
        read_meeting_record(path)
