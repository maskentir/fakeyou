import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.debrief.debrief import debrief_report_status, read_debrief
from fakeyou.debrief.debrief_schema import (
    DebriefItem,
    DebriefReport,
    DebriefValidationError,
    EvidenceSource,
    ItemKind,
    ItemStatus,
    SourceKind,
)

DebriefAvailability = Literal["available", "stale", "missing", "invalid"]
RegisterPosition = tuple[str, str, int]


def is_anonymous_speaker(participant_id: str) -> bool:
    if participant_id == "unknown":
        return True
    try:
        parsed = uuid.UUID(participant_id)
    except ValueError:
        return False
    return str(parsed) == participant_id.lower()


@dataclass(frozen=True, slots=True)
class MeetingScope:
    owner_id: str
    twin_id: str
    meeting_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class RegisterEvidence:
    entry_id: str
    quote: str
    source_kind: SourceKind
    participant_id: str
    speaker_identified: bool
    at: datetime


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    record_id: str
    scope: MeetingScope
    index: int
    kind: ItemKind
    status: ItemStatus
    text: str
    assignee_claim: str | None
    due_text: str | None
    evidence: tuple[RegisterEvidence, ...]
    generated_at: datetime

    @property
    def position(self) -> RegisterPosition:
        return (self.scope.created_at, self.scope.meeting_id, self.index)


@dataclass(frozen=True, slots=True)
class MeetingCoverage:
    scope: MeetingScope
    debrief: DebriefAvailability
    record_count: int
    source_limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RegisterQuery:
    meeting_id: str | None = None
    twin_id: str | None = None
    kind: ItemKind | None = None
    status: ItemStatus | None = None

    def covers(self, scope: MeetingScope) -> bool:
        return self.meeting_id in {None, scope.meeting_id} and self.twin_id in {
            None,
            scope.twin_id,
        }

    def selects(self, record: DecisionRecord) -> bool:
        return self.kind in {None, record.kind} and self.status in {None, record.status}


@dataclass(frozen=True, slots=True)
class DecisionRegister:
    records: tuple[DecisionRecord, ...]
    meetings: tuple[MeetingCoverage, ...]


@dataclass(frozen=True, slots=True)
class RegisterPage:
    records: tuple[DecisionRecord, ...]
    next_cursor: str | None


def _record_id(scope: MeetingScope, item: DebriefItem) -> str:
    identity = json.dumps(
        [
            scope.owner_id,
            scope.twin_id,
            scope.meeting_id,
            item.kind,
            item.status,
            item.text,
            [ref.entry_id for ref in item.evidence],
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _evidence(
    item: DebriefItem, sources: dict[str, EvidenceSource]
) -> tuple[RegisterEvidence, ...]:
    resolved: list[RegisterEvidence] = []
    for ref in item.evidence:
        source = sources.get(ref.entry_id)
        if source is None or ref.quote not in source.text:
            raise DebriefValidationError(ErrorCode.DEBRIEF_CANONICAL_INVALID)
        resolved.append(
            RegisterEvidence(
                entry_id=ref.entry_id,
                quote=ref.quote,
                source_kind=source.kind,
                participant_id=source.participant_id,
                speaker_identified=not is_anonymous_speaker(source.participant_id),
                at=source.at,
            )
        )
    return tuple(resolved)


def records_from_report(scope: MeetingScope, report: DebriefReport) -> tuple[DecisionRecord, ...]:
    if (report.owner_id, report.twin_id, report.meeting_id) != (
        scope.owner_id,
        scope.twin_id,
        scope.meeting_id,
    ):
        raise DebriefValidationError(ErrorCode.DEBRIEF_CANONICAL_INVALID)
    sources = {source.id: source for source in report.evidence_sources}
    records: dict[str, DecisionRecord] = {}
    for index, item in enumerate(report.items):
        record = DecisionRecord(
            record_id=_record_id(scope, item),
            scope=scope,
            index=index,
            kind=item.kind,
            status=item.status,
            text=item.text,
            assignee_claim=item.assignee_claim,
            due_text=item.due_text,
            evidence=_evidence(item, sources),
            generated_at=report.generated_at,
        )
        records.setdefault(record.record_id, record)
    return tuple(records.values())


def meeting_register(
    scope: MeetingScope, record_path: Path
) -> tuple[MeetingCoverage, tuple[DecisionRecord, ...]]:
    availability = debrief_report_status(record_path)
    if availability != "available":
        return MeetingCoverage(scope, availability, 0, ()), ()
    try:
        report = read_debrief(record_path)
        records = records_from_report(scope, report)
    except DebriefValidationError:
        return MeetingCoverage(scope, "invalid", 0, ()), ()
    return MeetingCoverage(scope, "available", len(records), report.source_limitations), records


def build_register(
    scopes: Sequence[MeetingScope],
    record_path_for: Callable[[str], Path],
    query: RegisterQuery,
) -> DecisionRegister:
    meetings: list[MeetingCoverage] = []
    records: list[DecisionRecord] = []
    ordered = sorted(scopes, key=lambda scope: (scope.created_at, scope.meeting_id))
    for scope in ordered:
        if not query.covers(scope):
            continue
        coverage, found = meeting_register(scope, record_path_for(scope.meeting_id))
        meetings.append(coverage)
        records.extend(record for record in found if query.selects(record))
    return DecisionRegister(tuple(records), tuple(meetings))


def _cursor(position: RegisterPosition) -> str:
    return base64.urlsafe_b64encode(json.dumps(position).encode("utf-8")).decode("ascii")


def _position(cursor: str) -> RegisterPosition:
    try:
        created_at, meeting_id, index = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (binascii.Error, UnicodeError, ValueError, TypeError):
        raise FakeYouError(ErrorCode.INPUT_INVALID, reason="cursor_invalid") from None
    if not (isinstance(created_at, str) and isinstance(meeting_id, str) and type(index) is int):
        raise FakeYouError(ErrorCode.INPUT_INVALID, reason="cursor_invalid")
    return (created_at, meeting_id, index)


def register_page(
    records: Sequence[DecisionRecord], *, cursor: str | None, limit: int
) -> RegisterPage:
    after = None if cursor is None else _position(cursor)
    remaining = [record for record in records if after is None or record.position > after]
    page = tuple(remaining[:limit])
    more = len(remaining) > limit
    return RegisterPage(page, _cursor(page[-1].position) if more else None)
