import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter
from fakeyou.persistence.record import (
    RECORD_SYNC_INTERVAL,
    MeetingRecord,
    ToolResultEntry,
    TranscriptEntry,
    TwinSpeechEntry,
    load_meeting_record,
    meeting_record_path,
    repaired_order,
)
from tests._persistence_fakes import RecordingJournal

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def transcript(index: int, text: str = "Hello.", **overrides: Any) -> TranscriptEvent:
    fields: dict[str, Any] = {
        "id": f"event-{index}",
        "timestamp": f"2026-09-30T10:00:0{index}+00:00",
        "source": "meeting_audio",
        "text": text,
        "finalized": True,
        "participant_id": "unknown",
        "speaker_evidence_id": None,
    }
    fields.update(overrides)
    return TranscriptEvent(**fields)


def open_record(tmp_path: Path, **overrides: Any) -> MeetingRecord:
    return MeetingRecord(
        meeting_record_path(tmp_path, SessionId("meeting-1")), now=lambda: NOW, **overrides
    )


def test_record_path_is_one_directory_per_meeting(tmp_path: Path) -> None:
    assert (
        meeting_record_path(tmp_path, SessionId("meeting-1"))
        == tmp_path / "meeting-1" / "record.jsonl"
    )


def test_record_roundtrips_transcripts_twin_speech_and_tool_results(tmp_path: Path) -> None:
    record = open_record(tmp_path)

    record.add_transcript(transcript(0, "When is the Orchid demo?", participant_id="Speaker A"))
    record.add_twin_speech("The demo is Friday at two in the afternoon.")
    record.add_tool_result(
        "recall_meeting_context",
        "From owner note on 2026-09-29 08:00 UTC: The demo is on Friday",
        812.5,
    )
    record.close()

    assert load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1"))) == [
        TranscriptEntry(
            at=NOW,
            id="event-0",
            spoken_at="2026-09-30T10:00:00+00:00",
            source="meeting_audio",
            text="When is the Orchid demo?",
            participant_id="Speaker A",
            speaker_evidence_id=None,
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

    lines = (
        meeting_record_path(tmp_path, SessionId("meeting-1"))
        .read_text(encoding="utf-8")
        .splitlines()
    )

    assert [json.loads(line)["kind"] for line in lines] == ["transcript", "twin_speech"]


def test_record_keeps_only_new_final_nonblank_transcripts(tmp_path: Path) -> None:
    record = open_record(tmp_path)

    record.add_transcript(transcript(0, "First."))
    record.add_transcript(transcript(0, "First."))
    record.add_transcript(transcript(1, "provisional", finalized=False))
    record.add_transcript(transcript(2, "   "))
    record.add_twin_speech("  ")
    record.close()

    entries = load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1")))
    assert [entry.id for entry in entries if isinstance(entry, TranscriptEntry)] == ["event-0"]
    assert len(entries) == 1


def test_record_write_failure_is_reported_once_without_raising(
    journal: RecordingJournal, tmp_path: Path
) -> None:
    record = open_record(tmp_path, faults=FaultReporter(journal=journal))
    record.close()

    record.add_transcript(transcript(0))
    record.add_twin_speech("Okay.")

    faults = journal.events()
    assert [(fault["source"], fault["kind"]) for fault in faults] == [("meeting_record", "unknown")]


def test_read_meeting_record_rejects_unknown_entries(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    path.parent.mkdir(parents=True)
    path.write_text('{"kind": "screenshot", "at": "2026-09-30T10:00:00+00:00"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="does not match any of the expected tags"):
        load_meeting_record(path)


def test_reopening_a_torn_record_drops_the_partial_line_before_appending(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    first = open_record(tmp_path)
    first.add_transcript(transcript(0))
    first.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "twin_speech", "te')
    journal = RecordingJournal()

    second = open_record(tmp_path, journal=journal)
    second.add_twin_speech("Okay.")
    second.close()

    assert [entry.kind for entry in load_meeting_record(path)] == ["transcript", "twin_speech"]
    assert any("record_tail_dropped" in line for line in journal.lines)


def test_loading_skips_a_torn_final_line_and_journals_it(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    first = open_record(tmp_path)
    first.add_transcript(transcript(0))
    first.close()
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "twin_speech", "te')
    journal = RecordingJournal()

    entries = load_meeting_record(path, journal=journal)

    assert [entry.kind for entry in entries] == ["transcript"]
    assert any("record_torn_line_dropped" in line for line in journal.lines)


def test_loading_still_rejects_corruption_before_the_final_line(tmp_path: Path) -> None:
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    path.parent.mkdir(parents=True)
    valid = open_record(tmp_path)
    valid.add_transcript(transcript(0))
    valid.close()
    line = path.read_text(encoding="utf-8")
    path.write_text(line + '{"kind": "twin_speech", "te\n' + line, encoding="utf-8")

    with pytest.raises(ValueError, match="validation error"):
        load_meeting_record(path)


def test_record_syncs_every_interval_before_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    synced: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    record = open_record(tmp_path)

    for index in range(RECORD_SYNC_INTERVAL - 1):
        record.add_transcript(transcript(index))
    assert synced == []

    record.add_transcript(transcript(RECORD_SYNC_INTERVAL - 1))
    assert len(synced) == 1
    record.close()


def test_record_roundtrips_a_resume_marker(tmp_path: Path) -> None:
    from fakeyou.persistence.record import ResumeEntry

    record = open_record(tmp_path)
    record.add_transcript(transcript(0))
    record.add_resume(attempt=2)
    record.close()

    entries = load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1")))
    assert entries[-1] == ResumeEntry(at=NOW, attempt=2)


def test_record_close_finalizes_and_second_close_is_safe(tmp_path: Path) -> None:
    record = open_record(tmp_path)
    record.add_transcript(transcript(0))

    record.close()
    record.close()

    assert len(load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1")))) == 1


def test_record_close_fsync_failure_propagates_and_second_close_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    record = open_record(tmp_path)
    record.add_transcript(transcript(0))

    def reject_fsync(_fd: int) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", reject_fsync)
    with pytest.raises(OSError, match="disk unavailable"):
        record.close()
    record.close()


def test_record_carries_speaker_identity_and_rejects_unattributed_lines(tmp_path: Path) -> None:
    from pydantic import ValidationError

    record = open_record(tmp_path)
    record.add_transcript(transcript(0, participant_id="a", speaker_evidence_id="e1"))
    record.close()
    [entry] = load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1")))
    assert isinstance(entry, TranscriptEntry)
    assert entry.participant_id == "a"
    assert entry.speaker_evidence_id == "e1"
    unattributed = {
        "kind": "transcript",
        "at": NOW.isoformat(),
        "id": "event-9",
        "spoken_at": "2026-09-30T10:00:09+00:00",
        "participant": "unknown",
        "source": "meeting_audio",
        "text": "Legacy.",
    }
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(unattributed) + "\n")
    with pytest.raises(ValidationError):
        load_meeting_record(path)


def test_repaired_order_sorts_stamped_transcripts_in_place(tmp_path: Path) -> None:
    record = open_record(tmp_path)
    record.add_transcript(transcript(1, "two", capture_ms=200.0))
    record.add_twin_speech("ack")
    record.add_transcript(transcript(0, "one", capture_ms=100.0))
    record.add_transcript(transcript(2, "old"))
    record.close()
    path = meeting_record_path(tmp_path, SessionId("meeting-1"))
    repaired = load_meeting_record(path)
    assert [entry.kind for entry in repaired] == [
        "transcript",
        "twin_speech",
        "transcript",
        "transcript",
    ]
    assert [entry.text for entry in repaired if entry.kind == "transcript"] == ["one", "two", "old"]
    second = repaired[1]
    assert isinstance(second, TwinSpeechEntry)
    assert second.text == "ack"
    raw = load_meeting_record(path, repair=False)
    assert [entry.text for entry in raw if entry.kind == "transcript"] == ["two", "one", "old"]


def test_record_accepts_visual_entry(tmp_path: Path) -> None:
    from fakeyou.persistence.record import VisualDraft, VisualEntry

    def draft(visual_id: str, caption: str, digest: str, page_ms: float = 0.0) -> VisualDraft:
        return VisualDraft(
            visual_id=visual_id,
            caption=caption,
            sha256=digest * 32,
            captured_at=NOW,
            frame_ref=f"frame:{digest * 4}@{NOW.isoformat()}",
            captured_page_ms=page_ms,
        )

    record = open_record(tmp_path)
    record.add_visual(
        draft("frame-7", "Quarterly revenue grew twelve percent.", "ab", 1727690400000.0)
    )
    record.add_visual(
        draft("frame-7", "Quarterly revenue grew twelve percent.", "ab", 1727690400000.0)
    )
    record.add_visual(draft("frame-8", "   ", "cd"))
    record.close()

    entries = load_meeting_record(meeting_record_path(tmp_path, SessionId("meeting-1")))
    assert entries == [
        VisualEntry(
            at=NOW,
            visual_id="frame-7",
            captured_at=NOW,
            captured_page_ms=1727690400000.0,
            sha256="ab" * 32,
            caption="Quarterly revenue grew twelve percent.",
            frame_ref=f"frame:{'ab' * 4}@{NOW.isoformat()}",
        )
    ]
    [line] = (
        meeting_record_path(tmp_path, SessionId("meeting-1"))
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert set(json.loads(line)) == {
        "kind",
        "at",
        "visual_id",
        "captured_at",
        "captured_page_ms",
        "sha256",
        "caption",
        "frame_ref",
    }


def test_repaired_order_is_stable_and_pure() -> None:
    first = TranscriptEntry(
        at=NOW,
        id="a",
        spoken_at="t",
        source="s",
        text="x",
        participant_id="p",
        speaker_evidence_id=None,
        stream_id="s0",
        capture_ms=10.0,
    )
    second = TranscriptEntry(
        at=NOW,
        id="b",
        spoken_at="t",
        source="s",
        text="y",
        participant_id="p",
        speaker_evidence_id=None,
        stream_id="s1",
        capture_ms=10.0,
    )
    entries = [second, first]
    ordered = cast(list[TranscriptEntry], repaired_order(entries))
    assert [entry.id for entry in ordered] == ["a", "b"]
    assert [entry.id for entry in entries] == ["b", "a"]
