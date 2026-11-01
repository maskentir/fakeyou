import json
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.persistence.layout import MeetingDir

RECORD_SYNC_INTERVAL = 20
RECORD_VERSION = 4
_TAIL_BLOCK_BYTES = 8192


class _Stamped(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: AwareDatetime


class _Entry(_Stamped):
    seq: int = Field(ge=1)
    causation_id: str | None = None


class RecordHeader(_Stamped):
    kind: Literal["header"] = "header"
    v: Literal[4] = 4
    attempt_id: str | None = None


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
    voice_clusters: tuple[str, ...] = ()


class TwinSpeechEntry(_Entry):
    kind: Literal["twin_speech"] = "twin_speech"
    text: str
    interrupted: bool = False
    planned: str | None = None


class ResumeEntry(_Entry):
    kind: Literal["resume"] = "resume"
    attempt: int
    attempt_id: str | None = None


class PerceptFields(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScreenFields(PerceptFields):
    ref: str
    shown_at: AwareDatetime
    sha256: str
    presenter_id: str | None = None
    presenter: str | None = None
    content: str
    title: str
    text: str
    key_points: tuple[str, ...] = ()
    figures: tuple[str, ...] = ()
    visuals: tuple[str, ...] = ()
    people: tuple[str, ...] = ()


class ScreenEntry(_Entry, ScreenFields):
    kind: Literal["screen"] = "screen"


class ChatFields(PerceptFields):
    ref: str
    message_id: str
    sender_id: str
    sender: str
    text: str
    sent_at: AwareDatetime
    own: bool = False
    backlog: bool = False


class ChatEntry(_Entry, ChatFields):
    kind: Literal["chat"] = "chat"


class SignalFields(PerceptFields):
    signal: str
    participant_id: str
    participant: str
    observed_at: AwareDatetime
    detail: str | None = None


class SignalEntry(_Entry, SignalFields):
    kind: Literal["signal"] = "signal"


RecordEntry = Annotated[
    TranscriptEntry | TwinSpeechEntry | ResumeEntry | ScreenEntry | ChatEntry | SignalEntry,
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


def record_header_of(line: str | bytes) -> RecordHeader | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict) or row.get("kind") != "header":
        return None
    if row.get("v") != RECORD_VERSION:
        raise FakeYouError(
            ErrorCode.RECORD_VERSION_UNSUPPORTED, found=row.get("v"), expected=RECORD_VERSION
        )
    return RecordHeader.model_validate(row)


def read_record_header(path: Path) -> RecordHeader | None:
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                return record_header_of(line)
    return None


def load_meeting_record(
    path: Path, *, repair: bool = True, journal: Journal | None = None
) -> list[RecordEntry]:
    return parse_meeting_record(path.read_text(encoding="utf-8"), repair=repair, journal=journal)


def parse_meeting_record(
    text: str, *, repair: bool = True, journal: Journal | None = None
) -> list[RecordEntry]:
    lines = [(index, line) for index, line in enumerate(text.splitlines()) if line.strip()]
    torn_tail = bool(text.rsplit("\n", 1)[-1].strip())
    if lines and record_header_of(lines[0][1]) is not None:
        lines = lines[1:]
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
        attempt_id: str | None = None,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        dropped = _drop_torn_tail(path)
        if dropped and journal is not None:
            journal.record("record_tail_dropped", source="meeting_record", bytes_dropped=dropped)
        self._entries: list[RecordEntry] = []
        if path.exists() and path.stat().st_size:
            read_record_header(path)
            self._entries = load_meeting_record(path, journal=journal)
        self._file = path.open("a", encoding="utf-8")
        self._faults = faults
        self._now = now
        self._header_pending = self._file.tell() == 0
        self._unsynced = 0
        self._seen = {entry.id for entry in self._entries if isinstance(entry, TranscriptEntry)}
        self._seen_chat = {
            entry.message_id for entry in self._entries if isinstance(entry, ChatEntry)
        }
        self._screens: dict[str, ScreenFields] = {}
        self._closed = False
        self._attempt_id = attempt_id
        self._seq = max((entry.seq for entry in self._entries), default=0)

    @property
    def attempt_id(self) -> str | None:
        return self._attempt_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def add_transcript(self, event: TranscriptEvent) -> None:
        if not event.finalized or not event.text.strip() or event.id in self._seen:
            return
        self._seen.add(event.id)
        self._append(
            TranscriptEntry(
                at=self._now(),
                seq=self._next_seq(),
                id=event.id,
                spoken_at=event.timestamp,
                source=event.source,
                text=event.text,
                participant_id=event.participant_id,
                speaker_evidence_id=event.speaker_evidence_id,
                stream_id=event.stream_id,
                capture_ms=event.capture_ms,
                display_claim=event.display_claim,
                voice_clusters=event.voice_clusters,
            )
        )

    def entries(self) -> tuple[RecordEntry, ...]:
        return tuple(self._entries)

    def add_twin_speech(
        self,
        text: str,
        *,
        interrupted: bool = False,
        planned: str | None = None,
        causation_id: str | None = None,
    ) -> None:
        if text.strip() or (planned or "").strip():
            self._append(
                TwinSpeechEntry(
                    at=self._now(),
                    seq=self._next_seq(),
                    causation_id=causation_id,
                    text=text,
                    interrupted=interrupted,
                    planned=planned,
                )
            )

    def add_resume(self, *, attempt: int) -> None:
        self._append(
            ResumeEntry(
                at=self._now(),
                seq=self._next_seq(),
                attempt=attempt,
                attempt_id=self._attempt_id,
            )
        )

    def add_screen(self, fields: ScreenFields) -> None:
        if not fields.ref.strip() or self._screens.get(fields.ref) == fields:
            return
        self._screens[fields.ref] = fields
        self._append(ScreenEntry(at=self._now(), seq=self._next_seq(), **fields.model_dump()))

    def add_chat(self, fields: ChatFields) -> None:
        if not fields.text.strip() or fields.message_id in self._seen_chat:
            return
        self._seen_chat.add(fields.message_id)
        self._append(ChatEntry(at=self._now(), seq=self._next_seq(), **fields.model_dump()))

    def add_signal(self, fields: SignalFields) -> None:
        self._append(SignalEntry(at=self._now(), seq=self._next_seq(), **fields.model_dump()))

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
        self._entries.append(entry)
        line = entry.model_dump_json() + "\n"
        if self._header_pending:
            header = RecordHeader(at=entry.at, attempt_id=self._attempt_id)
            line = header.model_dump_json() + "\n" + line
        try:
            self._file.write(line)
            self._file.flush()
            self._header_pending = False
            self._unsynced += 1
            if self._unsynced >= RECORD_SYNC_INTERVAL:
                os.fsync(self._file.fileno())
                self._unsynced = 0
        except (OSError, ValueError) as error:
            if self._faults is not None:
                self._faults.record(classify_exception("meeting_record", error))
