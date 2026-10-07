import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from fakeyou.conversation import TranscriptEvent
from fakeyou.faults import FaultReporter
from fakeyou.provider_faults import classify_exception

RECORD_FILE_NAME = "record.jsonl"


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: AwareDatetime


class TranscriptEntry(_Entry):
    kind: Literal["transcript"] = "transcript"
    id: str
    spoken_at: str
    source: str
    text: str
    participant_id: str
    speaker_evidence_id: str | None
    stream_id: str = ""
    capture_ms: float = 0.0


class TwinSpeechEntry(_Entry):
    kind: Literal["twin_speech"] = "twin_speech"
    text: str


class ToolResultEntry(_Entry):
    kind: Literal["tool_result"] = "tool_result"
    tool: str
    output: str
    duration_ms: float


class ResumeEntry(_Entry):
    kind: Literal["resume"] = "resume"
    attempt: int


RecordEntry = Annotated[
    TranscriptEntry | TwinSpeechEntry | ToolResultEntry | ResumeEntry,
    Field(discriminator="kind"),
]
_RECORD_ENTRY = TypeAdapter[RecordEntry](RecordEntry)


def meeting_record_path(root: Path, meeting_id: str) -> Path:
    return root / meeting_id / RECORD_FILE_NAME


def load_meeting_record(path: Path) -> list[RecordEntry]:
    return [
        _RECORD_ENTRY.validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class MeetingRecord:
    def __init__(
        self,
        path: Path,
        *,
        faults: FaultReporter | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")
        self._faults = faults
        self._now = now
        self._seen: set[str] = set()
        self._closed = False

    def add_transcript(self, event: TranscriptEvent) -> None:
        if not event.finalized or not event.text.strip() or event.id in self._seen:
            return
        self._seen.add(event.id)
        self._append(
            TranscriptEntry(
                at=self._now(),
                id=event.id,
                spoken_at=event.timestamp,
                source=event.source,
                text=event.text,
                participant_id=event.participant_id,
                speaker_evidence_id=event.speaker_evidence_id,
                stream_id=event.stream_id,
                capture_ms=event.capture_ms,
            )
        )

    def add_twin_speech(self, text: str) -> None:
        if text.strip():
            self._append(TwinSpeechEntry(at=self._now(), text=text))

    def add_tool_result(self, tool: str, output: str, duration_ms: float) -> None:
        self._append(
            ToolResultEntry(at=self._now(), tool=tool, output=output, duration_ms=duration_ms)
        )

    def add_resume(self, *, attempt: int) -> None:
        self._append(ResumeEntry(at=self._now(), attempt=attempt))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
        finally:
            self._file.close()

    def _append(self, entry: RecordEntry) -> None:
        try:
            self._file.write(entry.model_dump_json() + "\n")
            self._file.flush()
        except (OSError, ValueError) as error:
            if self._faults is not None:
                self._faults.record(classify_exception("meeting_record", error))
