import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fakeyou.debrief.debrief_sources import SourceEntry, load_record_snapshot
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import correction_uuid, write_correction_bundle
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.persistence.layout import DIARIZATION_JSON_FILE_NAME
from tests._debrief_sources_support import AT, encoded, transcript


def test_corrected_sidecar_attributes_quotes_to_corrected_speaker(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript(), transcript(id="entry-2", capture_ms=5000.0)))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [
            DiarizedTurn(start_ms=0, end_ms=3400, speaker="A"),
            DiarizedTurn(start_ms=3600, end_ms=7200, speaker="B"),
        ],
        clock=lambda: AT,
    )

    snapshot = load_record_snapshot(path)

    assert [entry.id for entry in snapshot.sources] == [
        "transcript:entry-1",
        "transcript:entry-2",
    ]
    assert [entry.participant_id for entry in snapshot.sources] == [
        correction_uuid("A", 0),
        correction_uuid("B", 3600),
    ]
    assert snapshot.record_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


def test_snapshot_without_sidecar_matches_live_label_baseline(tmp_path: Path) -> None:
    raw = encoded(transcript(), transcript(id="entry-2", participant_id="voice-a"))
    path = tmp_path / "record.jsonl"
    path.write_bytes(raw)

    snapshot = load_record_snapshot(path)

    assert snapshot.record_sha256 == hashlib.sha256(raw).hexdigest()
    assert snapshot.sources == (
        SourceEntry(
            id="transcript:entry-1",
            text="Please ask the owner about the new deadline.",
            kind="participant",
            participant_id="unknown",
            at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        ),
        SourceEntry(
            id="transcript:entry-2",
            text="Please ask the owner about the new deadline.",
            kind="participant",
            participant_id="voice-a",
            at=datetime(2026, 10, 3, 12, tzinfo=UTC),
        ),
    )


def test_stale_sidecar_falls_back_to_plain_record_and_records_fault(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")
    faults = FaultReporter(journal=Journal("meeting-1", emit=lambda _line: None))

    snapshot = load_record_snapshot(path, faults=faults)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]
    assert faults.total() == 1
    report = faults.report()
    assert report is not None
    kinds = report["faults"]["kinds"]
    assert kinds == [
        {
            "source": "meeting_record",
            "operation": None,
            "kind": "unknown",
            "count": 1,
            "status": None,
            "detail": "diarize.stale",
        }
    ]


def test_stale_sidecar_with_no_fault_reporter_uses_live_labels(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")

    snapshot = load_record_snapshot(path)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]


@pytest.mark.parametrize("sidecar", ["unsupported_schema", "corrupt_bytes"])
def test_unusable_sidecar_falls_back_to_live_labels_and_records_fault(
    tmp_path: Path, sidecar: str
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    if sidecar == "unsupported_schema":
        (path.parent / DIARIZATION_JSON_FILE_NAME).write_text(
            json.dumps({"schema": "diarization_correction/v9", "turns": []})
        )
    else:
        (path.parent / DIARIZATION_JSON_FILE_NAME).write_bytes(b"{invalid sidecar")
    faults = FaultReporter(journal=Journal("meeting-1", emit=lambda _line: None))

    snapshot = load_record_snapshot(path, faults=faults)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown"]
    assert faults.total() == 1
    report = faults.report()
    assert report is not None
    kinds = report["faults"]["kinds"]
    expected_detail = {
        "unsupported_schema": "diarize.schema_unsupported",
        "corrupt_bytes": "diarize.sidecar_invalid",
    }[sidecar]
    assert kinds == [
        {
            "source": "meeting_record",
            "operation": None,
            "kind": "unknown",
            "count": 1,
            "status": None,
            "detail": expected_detail,
        }
    ]


def test_corrupt_anchors_fall_back_to_live_labels_and_record_a_fault(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    (audio_dir / "anchors.jsonl").write_text("{corrupt\n", encoding="utf-8")
    faults = FaultReporter(journal=Journal("meeting-1", emit=lambda _line: None))

    snapshot = load_record_snapshot(path, faults=faults)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown"]
    assert faults.total() == 1
    report = faults.report()
    assert report is not None
    assert report["faults"]["kinds"][0]["detail"] == "diarize.input_unreadable"


def test_stale_sidecar_without_fault_reporter_emits_journal_fallback(tmp_path: Path) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(transcript(id="entry-2")) + "\n")
    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)

    snapshot = load_record_snapshot(path, journal=journal)

    assert [entry.participant_id for entry in snapshot.sources] == ["unknown", "unknown"]
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "fault_recorded"
    assert row["source"] == "meeting_record"
    assert row["kind"] == "unknown"
    assert row["detail"] == "diarize.stale"


def test_unexpected_correction_errors_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.jsonl"
    path.write_bytes(encoded(transcript()))

    def boom(_record_path: Path) -> list[Any]:
        raise RuntimeError("correction backend failed")

    monkeypatch.setattr("fakeyou.debrief.debrief_sources.load_corrected_record", boom)

    with pytest.raises(RuntimeError):
        load_record_snapshot(path)
