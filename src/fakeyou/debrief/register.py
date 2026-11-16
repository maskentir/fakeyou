import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
Attribution = Literal["recorded_label", "unattributed"]
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
    attribution: Attribution
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
    source_fingerprint: str

    @property
    def position(self) -> RegisterPosition:
        return (self.scope.created_at, self.scope.meeting_id, self.index)

    @property
    def page_key(self) -> str:
        return self.record_id


class Paged(Protocol):
    @property
    def position(self) -> RegisterPosition: ...

    @property
    def page_key(self) -> str: ...


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

    def fingerprint(self, owner_id: str) -> str:
        return short_digest([owner_id, self.meeting_id, self.twin_id, self.kind, self.status])


@dataclass(frozen=True, slots=True)
class DecisionRegister:
    records: tuple[DecisionRecord, ...]
    meetings: tuple[MeetingCoverage, ...]


@dataclass(frozen=True, slots=True)
class RegisterPage[T: Paged]:
    records: tuple[T, ...]
    next_cursor: str | None


def short_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _record_id(scope: MeetingScope, item: DebriefItem) -> str:
    return short_digest(
        [scope.owner_id, scope.twin_id, scope.meeting_id, item.model_dump(mode="json")]
    )


def _source_fingerprint(
    record_id: str, scope: MeetingScope, item: DebriefItem, sources: dict[str, EvidenceSource]
) -> str:
    return short_digest(
        [
            record_id,
            scope.created_at,
            [
                sources[ref.entry_id].model_dump(mode="json") | {"quote": ref.quote}
                for ref in item.evidence
            ],
        ]
    )


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
                attribution="unattributed"
                if is_anonymous_speaker(source.participant_id)
                else "recorded_label",
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
        record_id = _record_id(scope, item)
        record = DecisionRecord(
            record_id=record_id,
            scope=scope,
            index=index,
            kind=item.kind,
            status=item.status,
            text=item.text,
            assignee_claim=item.assignee_claim,
            due_text=item.due_text,
            evidence=_evidence(item, sources),
            generated_at=report.generated_at,
            source_fingerprint=_source_fingerprint(record_id, scope, item, sources),
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


class RegisterCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    after: tuple[str, str, int]
    query: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    results: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]

    def encode(self) -> str:
        return base64.urlsafe_b64encode(self.model_dump_json().encode("utf-8")).decode("ascii")

    @classmethod
    def decode(cls, cursor: str) -> "RegisterCursor":
        try:
            return cls.model_validate_json(base64.urlsafe_b64decode(cursor.encode("ascii")))
        except (binascii.Error, UnicodeError, ValidationError):
            raise FakeYouError(ErrorCode.INPUT_INVALID, reason="cursor_invalid") from None


def results_fingerprint(records: Sequence[Paged]) -> str:
    return short_digest([record.page_key for record in records])


def register_page[T: Paged](
    records: Sequence[T], *, cursor: str | None, limit: int, query: str
) -> RegisterPage[T]:
    results = results_fingerprint(records)
    after: RegisterPosition | None = None
    if cursor is not None:
        decoded = RegisterCursor.decode(cursor)
        if decoded.query != query:
            raise FakeYouError(ErrorCode.INPUT_INVALID, reason="cursor_query_mismatch")
        if decoded.results != results:
            raise FakeYouError(ErrorCode.INPUT_INVALID, reason="cursor_stale")
        after = decoded.after
    remaining = [record for record in records if after is None or record.position > after]
    page = tuple(remaining[:limit])
    if len(remaining) <= limit:
        return RegisterPage(page, None)
    following = RegisterCursor(after=page[-1].position, query=query, results=results)
    return RegisterPage(page, following.encode())
