from dataclasses import dataclass
from pathlib import Path

from fakeyou.config.identifiers import SessionId, parse_session_id

RECORD_FILE_NAME = "record.jsonl"
INGESTION_FILE_NAME = "ingestion.json"
ACTIONS_FILE_NAME = "actions.json"
LEDGER_LOCK_FILE_NAME = "actions.lock"
CONTEXT_FILE_NAME = "meeting-context.json"
EVIDENCE_FILE_NAME = "participants.jsonl"
PROBE_FILE_NAME = "attribution-probe.jsonl"
DIARIZATION_JSON_FILE_NAME = "diarization.json"
DIARIZATION_RTTM_FILE_NAME = "diarization.rttm"
CORRECTION_LABELS_FILE_NAME = "correction_labels.json"
REPORT_FILE_NAME = "debrief.json"
MARKDOWN_FILE_NAME = "debrief.md"
ATTEMPT_FILE_NAME = "debrief-attempt.json"
SLOTS_FILE_NAME = "slots.json"
ANCHORS_FILE_NAME = "anchors.jsonl"
AUDIO_DIR_NAME = "audio"
PERCEPTION_DIR_NAME = "perception"
SCREENS_DIR_NAME = "screens"
LOCK_FILE_SUFFIX = ".lock"


@dataclass(frozen=True, slots=True)
class MeetingDir:
    root: Path
    meeting_id: SessionId

    @classmethod
    def from_record_path(cls, record_path: Path) -> "MeetingDir":
        return cls(
            record_path.parent.parent,
            parse_session_id(record_path.parent.name, field="meeting_id"),
        )

    @property
    def dir(self) -> Path:
        return self.root / self.meeting_id

    @property
    def record(self) -> Path:
        return self.dir / RECORD_FILE_NAME

    @property
    def ingestion(self) -> Path:
        return self.dir / INGESTION_FILE_NAME

    @property
    def actions(self) -> Path:
        return self.dir / ACTIONS_FILE_NAME

    @property
    def actions_lock(self) -> Path:
        return self.dir / LEDGER_LOCK_FILE_NAME

    @property
    def context(self) -> Path:
        return self.dir / CONTEXT_FILE_NAME

    @property
    def participants(self) -> Path:
        return self.dir / EVIDENCE_FILE_NAME

    @property
    def probe(self) -> Path:
        return self.dir / PROBE_FILE_NAME

    @property
    def diarization(self) -> Path:
        return self.dir / DIARIZATION_JSON_FILE_NAME

    @property
    def diarization_rttm(self) -> Path:
        return self.dir / DIARIZATION_RTTM_FILE_NAME

    @property
    def correction_labels(self) -> Path:
        return self.dir / CORRECTION_LABELS_FILE_NAME

    @property
    def debrief_json(self) -> Path:
        return self.dir / REPORT_FILE_NAME

    @property
    def debrief_md(self) -> Path:
        return self.dir / MARKDOWN_FILE_NAME

    @property
    def debrief_attempt(self) -> Path:
        return self.dir / ATTEMPT_FILE_NAME

    @property
    def audio_dir(self) -> Path:
        return self.dir / AUDIO_DIR_NAME

    @property
    def perception_dir(self) -> Path:
        return self.dir / PERCEPTION_DIR_NAME

    @property
    def screens_dir(self) -> Path:
        return self.perception_dir / SCREENS_DIR_NAME

    @property
    def slots(self) -> Path:
        return self.audio_dir / SLOTS_FILE_NAME

    @property
    def lock(self) -> Path:
        return self.root / f"{self.meeting_id}{LOCK_FILE_SUFFIX}"


def meeting_profile_dir(root: Path, *, twin_id: SessionId, meeting_id: SessionId) -> Path:
    return root / twin_id / meeting_id
