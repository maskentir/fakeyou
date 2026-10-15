import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.persistence.layout import MeetingDir

RECORD_SYNC_INTERVAL = 20
_TAIL_BLOCK_BYTES = 8192


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
    display_claim: str | None = None
    corrected_speaker_uuid: str | None = None


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


@dataclass(frozen=True, slots=True)
class VisualDraft:
    visual_id: str
    caption: str
    sha256: str
    captured_at: datetime
    frame_ref: str
    captured_page_ms: float = 0.0


class VisualEntry(_Entry):
    kind: Literal["visual"] = "visual"
    visual_id: str
    captured_at: AwareDatetime
    sha256: str
    caption: str
    frame_ref: str
    captured_page_ms: float = 0.0


RecordEntry = Annotated[
    TranscriptEntry | TwinSpeechEntry | ToolResultEntry | ResumeEntry | VisualEntry,
    Field(discriminator="kind"),
]
_RECORD_ENTRY = TypeAdapter[RecordEntry](RecordEntry)


def meeting_record_path(root: Path, meeting_id: SessionId) -> Path:
    return MeetingDir(root, meeting_id).record


def repaired_order(entries: Sequence[RecordEntry]) -> list[RecordEntry]:
    stamped = sorted(
        (
            entry
            for entry in entries
            if isinstance(entry, TranscriptEntry) and entry.capture_ms > 0.0
        ),
        key=lambda entry: (entry.capture_ms, entry.spoken_at, entry.id),
    )
    fixed = list(entries)
    ordered = iter(stamped)
    for index, entry in enumerate(fixed):
        if isinstance(entry, TranscriptEntry) and entry.capture_ms > 0.0:
            fixed[index] = next(ordered)
    return fixed


def load_meeting_record(
    path: Path, *, repair: bool = True, journal: Journal | None = None
) -> list[RecordEntry]:
    text = path.read_text(encoding="utf-8")
    lines = [(index, line) for index, line in enumerate(text.splitlines()) if line.strip()]
    torn_tail = bool(text.rsplit("\n", 1)[-1].strip())
    entries: list[RecordEntry] = []
    for index, line in lines:
        if torn_tail and index == lines[-1][0]:
            try:
                entries.append(_RECORD_ENTRY.validate_json(line))
            except ValueError:
                if journal is not None:
                    journal.record(
                        "record_torn_line_dropped", source="meeting_record", bytes_skipped=len(line)
                    )
            break
        entries.append(_RECORD_ENTRY.validate_json(line))
    return repaired_order(entries) if repair else entries


def _drop_torn_tail(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r+b") as handle:
        size = handle.seek(0, os.SEEK_END)
        if size == 0:
            return 0
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return 0
        end = size
        cut = 0
        while end > 0:
            start = max(0, end - _TAIL_BLOCK_BYTES)
            handle.seek(start)
            position = handle.read(end - start).rfind(b"\n")
            if position != -1:
                cut = start + position + 1
                break
            end = start
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())
    return size - cut


class MeetingRecord:
    def __init__(
        self,
        path: Path,
        *,
        faults: FaultReporter | None = None,
        journal: Journal | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        dropped = _drop_torn_tail(path)
        if dropped and journal is not None:
            journal.record("record_tail_dropped", source="meeting_record", bytes_dropped=dropped)
        self._file = path.open("a", encoding="utf-8")
        self._faults = faults
        self._now = now
        self._unsynced = 0
        self._seen: set[str] = set()
        self._seen_visual: set[str] = set()
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
                display_claim=event.display_claim,
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

    def add_visual(self, draft: VisualDraft) -> None:
        if (
            not draft.visual_id.strip()
            or not draft.caption.strip()
            or not draft.frame_ref.strip()
            or draft.visual_id in self._seen_visual
        ):
            return
        self._seen_visual.add(draft.visual_id)
        self._append(
            VisualEntry(
                at=self._now(),
                visual_id=draft.visual_id,
                captured_at=draft.captured_at,
                sha256=draft.sha256,
                caption=draft.caption,
                frame_ref=draft.frame_ref,
                captured_page_ms=draft.captured_page_ms,
            )
        )

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
            self._unsynced += 1
            if self._unsynced >= RECORD_SYNC_INTERVAL:
                os.fsync(self._file.fileno())
                self._unsynced = 0
        except (OSError, ValueError) as error:
            if self._faults is not None:
                self._faults.record(classify_exception("meeting_record", error))
