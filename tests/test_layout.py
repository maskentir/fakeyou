from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.persistence.layout import (
    ACTIONS_FILE_NAME,
    ATTEMPT_FILE_NAME,
    AUDIO_DIR_NAME,
    CONTEXT_FILE_NAME,
    CORRECTION_LABELS_FILE_NAME,
    DIARIZATION_JSON_FILE_NAME,
    DIARIZATION_RTTM_FILE_NAME,
    EVIDENCE_FILE_NAME,
    INGESTION_FILE_NAME,
    LEDGER_LOCK_FILE_NAME,
    LOCK_FILE_SUFFIX,
    MARKDOWN_FILE_NAME,
    PROBE_FILE_NAME,
    RECORD_FILE_NAME,
    REPORT_FILE_NAME,
    SLOTS_FILE_NAME,
    VISUAL_DIR_NAME,
    MeetingDir,
    meeting_profile_dir,
)


def test_meeting_dir_builds_every_path_under_the_meeting_directory(tmp_path: Path) -> None:
    layout = MeetingDir(tmp_path, SessionId("meeting-9"))

    assert layout.dir == tmp_path / "meeting-9"
    assert layout.record == layout.dir / "record.jsonl"
    assert layout.ingestion == layout.dir / "ingestion.json"
    assert layout.actions == layout.dir / "actions.json"
    assert layout.actions_lock == layout.dir / "actions.lock"
    assert layout.context == layout.dir / "meeting-context.json"
    assert layout.participants == layout.dir / "participants.jsonl"
    assert layout.probe == layout.dir / "attribution-probe.jsonl"
    assert layout.diarization == layout.dir / "diarization.json"
    assert layout.diarization_rttm == layout.dir / "diarization.rttm"
    assert layout.correction_labels == layout.dir / "correction_labels.json"
    assert layout.debrief_json == layout.dir / "debrief.json"
    assert layout.debrief_md == layout.dir / "debrief.md"
    assert layout.debrief_attempt == layout.dir / "debrief-attempt.json"
    assert layout.audio_dir == layout.dir / "audio"
    assert layout.visual_dir == layout.dir / "visual"
    assert layout.slots == layout.dir / "audio" / "slots.json"
    assert layout.lock == tmp_path / "meeting-9.lock"


def test_meeting_dir_file_names_are_the_single_source() -> None:
    assert (
        RECORD_FILE_NAME,
        INGESTION_FILE_NAME,
        ACTIONS_FILE_NAME,
        LEDGER_LOCK_FILE_NAME,
        CONTEXT_FILE_NAME,
        EVIDENCE_FILE_NAME,
        PROBE_FILE_NAME,
        DIARIZATION_JSON_FILE_NAME,
        DIARIZATION_RTTM_FILE_NAME,
        CORRECTION_LABELS_FILE_NAME,
        REPORT_FILE_NAME,
        MARKDOWN_FILE_NAME,
        ATTEMPT_FILE_NAME,
        SLOTS_FILE_NAME,
        AUDIO_DIR_NAME,
        VISUAL_DIR_NAME,
        LOCK_FILE_SUFFIX,
    ) == (
        "record.jsonl",
        "ingestion.json",
        "actions.json",
        "actions.lock",
        "meeting-context.json",
        "participants.jsonl",
        "attribution-probe.jsonl",
        "diarization.json",
        "diarization.rttm",
        "correction_labels.json",
        "debrief.json",
        "debrief.md",
        "debrief-attempt.json",
        "slots.json",
        "audio",
        "visual",
        ".lock",
    )


def test_from_record_path_rebuilds_the_layout(tmp_path: Path) -> None:
    record = tmp_path / "meeting-9" / "record.jsonl"

    assert MeetingDir.from_record_path(record) == MeetingDir(tmp_path, SessionId("meeting-9"))


def test_from_record_path_rejects_unvalidated_names(tmp_path: Path) -> None:
    with pytest.raises(FakeYouError, match="invalid") as error:
        MeetingDir.from_record_path(tmp_path / ".." / "record.jsonl")

    assert error.value.code is ErrorCode.SESSION_ID_INVALID


def test_meeting_profile_dir_nests_twin_and_meeting(tmp_path: Path) -> None:
    assert (
        meeting_profile_dir(
            tmp_path, twin_id=SessionId("twin-1"), meeting_id=SessionId("meeting-9")
        )
        == tmp_path / "twin-1" / "meeting-9"
    )
