import json
from pathlib import Path

import fakeyou.debrief.debrief as module
from fakeyou.diarization.correction_labels import (
    describe_correction_speakers,
    write_correction_labels,
)
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import (
    correction_digest,
    correction_uuid,
    write_correction_bundle,
)
from fakeyou.observability.journal import Journal
from fakeyou.persistence.layout import MARKDOWN_FILE_NAME
from tests._debrief_support import AT, FIXTURE, report_for
from tests._debrief_support import (
    record_path as record_path,
)


def test_sidecar_change_marks_cached_report_stale(record_path: Path) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    assert module.debrief_report_status(record_path) == "available"
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )

    assert module.debrief_report_status(record_path) == "stale"


def test_status_check_reports_stale_sidecar_fallback_to_journal(record_path: Path) -> None:
    report = report_for(record_path)
    module.write_debrief(record_path, report)
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "kind": "transcript",
                    "at": AT.isoformat(),
                    "id": "e2",
                    "spoken_at": "12:01:00",
                    "source": "caption",
                    "text": "A second voice notes the deadline.",
                    "participant_id": "unknown",
                    "speaker_evidence_id": None,
                }
            )
            + "\n"
        )
    lines: list[str] = []
    journal = Journal("b2-meeting", emit=lines.append)

    assert module.debrief_report_status(record_path, journal=journal) == "stale"

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "fault_recorded"
    assert row["source"] == "meeting_record"
    assert row["detail"] == "diarize.stale"


def test_write_debrief_autoloads_correction_labels_into_markdown(record_path: Path) -> None:
    audio_dir = record_path.parent / "audio"
    audio_dir.mkdir()
    write_correction_bundle(
        audio_dir,
        record_path,
        [DiarizedTurn(start_ms=0, end_ms=3400, speaker="A")],
        clock=lambda: AT,
    )
    uuid = correction_uuid("A", 0)
    write_correction_labels(record_path, {uuid: FIXTURE["render_shown"]})
    report = report_for(record_path, correction_sha256=correction_digest(record_path))
    module.write_debrief(record_path, report)
    markdown = record_path.with_name(MARKDOWN_FILE_NAME).read_text(encoding="utf-8")
    expected = f"Speaker A, shown as {FIXTURE['render_shown']}, unverified"

    assert report.evidence_sources[0].participant_id == uuid
    assert describe_correction_speakers(record_path) == {uuid: expected}
    assert expected in markdown
