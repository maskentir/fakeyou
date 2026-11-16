import hashlib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import IDENTIFIER_BODY
from fakeyou.debrief.register import (
    DebriefAvailability,
    DecisionRecord,
    DecisionRegister,
    MeetingCoverage,
    MeetingScope,
    RegisterPosition,
    RegisterQuery,
    build_register,
    short_digest,
)
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.ingestion_status import RecordLockError, record_lock
from fakeyou.persistence.layout import MeetingDir

LINEAGE_SCHEMA_VERSION = "decision_lineage/v1"
WINDOW_MEETINGS = 20
WINDOW_RECORDS = 200
MAX_COMPARISONS = 2000

Relationship = Literal[
    "related_discussion",
    "potential_contradiction",
    "potential_supersession",
    "no_established_relationship",
]
LineageMethod = Literal["manual_curated"]
LineageAvailability = Literal["available", "missing", "invalid"]
Direction = Literal["incoming", "outgoing"]
ComparisonState = Literal["current", "unresolved", "invalidated"]
LineageOutcome = Literal["created", "unchanged", "replaced"]

_Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
_Identifier = Annotated[str, StringConstraints(pattern=rf"^{IDENTIFIER_BODY}$", max_length=128)]
_STRICT = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class CuratedComparison(BaseModel):
    model_config = _STRICT

    earlier_record_id: _Digest
    later_record_id: _Digest
    relationship: Relationship


class LineageImport(BaseModel):
    model_config = _STRICT

    schema_version: Literal["decision_lineage/v1"]
    owner_id: _Identifier
    twin_id: _Identifier
    meeting_id: _Identifier
    method: LineageMethod
    comparisons: Annotated[
        tuple[CuratedComparison, ...], Field(min_length=1, max_length=MAX_COMPARISONS)
    ]
    replaces: _Digest | None = None


class BoundComparison(BaseModel):
    model_config = _STRICT

    earlier_record_id: _Digest
    earlier_meeting_id: _Identifier
    earlier_fingerprint: _Digest
    later_record_id: _Digest
    later_fingerprint: _Digest
    relationship: Relationship


class DecisionLineage(BaseModel):
    model_config = _STRICT

    schema_version: Literal["decision_lineage/v1"]
    owner_id: _Identifier
    twin_id: _Identifier
    meeting_id: _Identifier
    method: LineageMethod
    imported_at: AwareDatetime
    comparisons: Annotated[
        tuple[BoundComparison, ...], Field(min_length=1, max_length=MAX_COMPARISONS)
    ]

    @model_validator(mode="after")
    def every_pair_is_distinct_and_spans_two_meetings(self) -> Self:
        pairs = [(item.earlier_record_id, item.later_record_id) for item in self.comparisons]
        if len(set(pairs)) != len(pairs):
            raise ValueError("a pair is compared more than once")
        if any(
            item.earlier_meeting_id == self.meeting_id
            or item.earlier_record_id == item.later_record_id
            for item in self.comparisons
        ):
            raise ValueError("a comparison must span two meetings")
        return self

    def same_comparisons(self, other: "DecisionLineage") -> bool:
        def ordered(lineage: DecisionLineage) -> list[tuple[str, ...]]:
            return sorted(tuple(item.model_dump().values()) for item in lineage.comparisons)

        return self.method == other.method and ordered(self) == ordered(other)

    def scope_matches(self, scope: MeetingScope) -> bool:
        return (self.owner_id, self.twin_id, self.meeting_id) == (
            scope.owner_id,
            scope.twin_id,
            scope.meeting_id,
        )


@dataclass(frozen=True, slots=True)
class LineageSource:
    scope: MeetingScope
    availability: LineageAvailability
    lineage: DecisionLineage | None
    revision: str | None


@dataclass(frozen=True, slots=True)
class LineagePublication:
    lineage: DecisionLineage
    revision: str
    outcome: LineageOutcome


@dataclass(frozen=True, slots=True)
class LineageEntry:
    relation_id: str
    relationship: Relationship
    direction: Direction
    earlier: DecisionRecord
    later: DecisionRecord
    method: LineageMethod
    imported_at: datetime

    @property
    def other(self) -> DecisionRecord:
        return self.later if self.direction == "outgoing" else self.earlier

    @property
    def position(self) -> RegisterPosition:
        return self.other.position

    @property
    def page_key(self) -> str:
        return self.relation_id


@dataclass(frozen=True, slots=True)
class LineageAccounting:
    related_discussion: int
    potential_contradiction: int
    potential_supersession: int
    no_established_relationship: int
    unresolved: int
    invalidated: int
    not_compared: int


@dataclass(frozen=True, slots=True)
class LineageCoverage:
    scope: MeetingScope
    debrief: DebriefAvailability
    lineage: LineageAvailability
    lineage_revision: str | None
    source_limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordLineage:
    record: DecisionRecord
    entries: tuple[LineageEntry, ...]
    accounting: LineageAccounting
    meetings: tuple[LineageCoverage, ...]


def _rejected(reason: str) -> FakeYouError:
    return FakeYouError(ErrorCode.INPUT_INVALID, reason=reason)


def meeting_time(scope: MeetingScope) -> datetime | None:
    try:
        at = datetime.fromisoformat(scope.created_at)
    except ValueError:
        return None
    return at if at.utcoffset() is not None else None


def same_twin(first: MeetingScope, second: MeetingScope) -> bool:
    return (first.owner_id, first.twin_id) == (second.owner_id, second.twin_id)


def strictly_earlier(earlier: MeetingScope, later: MeetingScope) -> bool:
    earlier_at, later_at = meeting_time(earlier), meeting_time(later)
    return earlier_at is not None and later_at is not None and earlier_at < later_at


def comparison_window(later: MeetingScope, register: DecisionRegister) -> frozenset[str]:
    later_at = meeting_time(later)
    if later_at is None:
        return frozenset()
    earlier = sorted(
        (
            (at, coverage.scope.meeting_id)
            for coverage in register.meetings
            if coverage.debrief == "available"
            and same_twin(coverage.scope, later)
            and (at := meeting_time(coverage.scope)) is not None
            and at < later_at
        ),
        reverse=True,
    )[:WINDOW_MEETINGS]
    rank = {meeting_id: order for order, (_, meeting_id) in enumerate(earlier)}
    eligible = sorted(
        (record for record in register.records if record.scope.meeting_id in rank),
        key=lambda record: (rank[record.scope.meeting_id], record.index),
    )
    return frozenset(record.record_id for record in eligible[:WINDOW_RECORDS])


def _bind(
    comparison: CuratedComparison,
    records: dict[str, DecisionRecord],
    later_scope: MeetingScope,
    window: frozenset[str],
) -> BoundComparison:
    if comparison.earlier_record_id == comparison.later_record_id:
        raise _rejected("lineage_self_pair")
    earlier = records.get(comparison.earlier_record_id)
    later = records.get(comparison.later_record_id)
    if earlier is None or later is None:
        raise _rejected("lineage_record_unknown")
    if later.scope.meeting_id != later_scope.meeting_id:
        raise _rejected("lineage_later_record_outside_meeting")
    if earlier.scope.meeting_id == later.scope.meeting_id:
        raise _rejected("lineage_same_meeting")
    if not same_twin(earlier.scope, later.scope):
        raise _rejected("lineage_cross_twin")
    if not strictly_earlier(earlier.scope, later.scope):
        raise _rejected("lineage_order_unverified")
    if earlier.record_id not in window:
        raise _rejected("lineage_outside_window")
    return BoundComparison(
        earlier_record_id=earlier.record_id,
        earlier_meeting_id=earlier.scope.meeting_id,
        earlier_fingerprint=earlier.source_fingerprint,
        later_record_id=later.record_id,
        later_fingerprint=later.source_fingerprint,
        relationship=comparison.relationship,
    )


def bind_lineage(
    document: LineageImport,
    *,
    owner_id: str,
    register: DecisionRegister,
    imported_at: datetime,
) -> DecisionLineage:
    coverage = next(
        (item for item in register.meetings if item.scope.meeting_id == document.meeting_id),
        None,
    )
    if coverage is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    if (document.owner_id, document.twin_id) != (owner_id, coverage.scope.twin_id):
        raise _rejected("lineage_scope_mismatch")
    if coverage.debrief != "available":
        raise _rejected("lineage_source_unavailable")
    records = {record.record_id: record for record in register.records}
    window = comparison_window(coverage.scope, register)
    labels: dict[tuple[str, str], Relationship] = {}
    bound: list[BoundComparison] = []
    for comparison in document.comparisons:
        pair = (comparison.earlier_record_id, comparison.later_record_id)
        if pair in labels:
            same = labels[pair] == comparison.relationship
            raise _rejected("lineage_duplicate_pair" if same else "lineage_conflicting_labels")
        labels[pair] = comparison.relationship
        bound.append(_bind(comparison, records, coverage.scope, window))
    return DecisionLineage(
        schema_version="decision_lineage/v1",
        owner_id=owner_id,
        twin_id=coverage.scope.twin_id,
        meeting_id=coverage.scope.meeting_id,
        method=document.method,
        imported_at=imported_at,
        comparisons=tuple(bound),
    )


def lineage_path(record_path: Path) -> Path:
    return MeetingDir.from_record_path(record_path).decision_lineage


def lineage_revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:32]


def publish_lineage(record_path: Path, lineage: DecisionLineage) -> str:
    raw = lineage.model_dump_json().encode("utf-8")
    write_atomically(lineage_path(record_path), raw)
    return lineage_revision(raw)


def read_lineage(scope: MeetingScope, record_path: Path) -> LineageSource:
    try:
        raw = lineage_path(record_path).read_bytes()
    except FileNotFoundError:
        return LineageSource(scope, "missing", None, None)
    revision = lineage_revision(raw)
    try:
        lineage = DecisionLineage.model_validate_json(raw)
    except ValidationError:
        return LineageSource(scope, "invalid", None, revision)
    if not lineage.scope_matches(scope):
        return LineageSource(scope, "invalid", None, revision)
    return LineageSource(scope, "available", lineage, revision)


def _relation_id(lineage: DecisionLineage, comparison: BoundComparison) -> str:
    return short_digest(
        [
            LINEAGE_SCHEMA_VERSION,
            lineage.owner_id,
            lineage.twin_id,
            comparison.earlier_record_id,
            comparison.earlier_fingerprint,
            comparison.later_record_id,
            comparison.later_fingerprint,
            comparison.relationship,
        ]
    )


def _resolve(
    lineage: DecisionLineage,
    comparison: BoundComparison,
    records: dict[str, DecisionRecord],
    target: DecisionRecord,
) -> tuple[ComparisonState, LineageEntry | None]:
    earlier = records.get(comparison.earlier_record_id)
    later = records.get(comparison.later_record_id)
    if (
        earlier is None
        or later is None
        or earlier.scope.meeting_id != comparison.earlier_meeting_id
        or not lineage.scope_matches(later.scope)
        or not same_twin(earlier.scope, later.scope)
    ):
        return "unresolved", None
    if (earlier.source_fingerprint, later.source_fingerprint) != (
        comparison.earlier_fingerprint,
        comparison.later_fingerprint,
    ) or not strictly_earlier(earlier.scope, later.scope):
        return "invalidated", None
    entry = LineageEntry(
        relation_id=_relation_id(lineage, comparison),
        relationship=comparison.relationship,
        direction="outgoing" if target.record_id == earlier.record_id else "incoming",
        earlier=earlier,
        later=later,
        method=lineage.method,
        imported_at=lineage.imported_at,
    )
    return "current", entry


def _coverage(coverage: MeetingCoverage, source: LineageSource) -> LineageCoverage:
    return LineageCoverage(
        scope=coverage.scope,
        debrief=coverage.debrief,
        lineage=source.availability,
        lineage_revision=source.revision,
        source_limitations=coverage.source_limitations,
    )


def record_lineage(
    target: DecisionRecord, register: DecisionRegister, sources: Sequence[LineageSource]
) -> RecordLineage:
    records = {record.record_id: record for record in register.records}
    entries: list[LineageEntry] = []
    states: Counter[ComparisonState] = Counter()
    compared: set[str] = set()
    for source in sources:
        if source.lineage is None or not same_twin(source.scope, target.scope):
            continue
        for comparison in source.lineage.comparisons:
            pair = (comparison.earlier_record_id, comparison.later_record_id)
            if target.record_id not in pair:
                continue
            state, entry = _resolve(source.lineage, comparison, records, target)
            states[state] += 1
            if state != "unresolved":
                compared.update(pair)
            if entry is not None:
                entries.append(entry)
    labels = Counter(entry.relationship for entry in entries)
    not_compared = sum(
        1
        for record in register.records
        if same_twin(record.scope, target.scope)
        and record.scope.meeting_id != target.scope.meeting_id
        and record.record_id not in compared
    )
    lineage_of = {source.scope.meeting_id: source for source in sources}
    return RecordLineage(
        record=target,
        entries=tuple(sorted(entries, key=lambda entry: entry.position)),
        accounting=LineageAccounting(
            related_discussion=labels["related_discussion"],
            potential_contradiction=labels["potential_contradiction"],
            potential_supersession=labels["potential_supersession"],
            no_established_relationship=labels["no_established_relationship"],
            unresolved=states["unresolved"],
            invalidated=states["invalidated"],
            not_compared=not_compared,
        ),
        meetings=tuple(
            _coverage(
                coverage,
                lineage_of.get(coverage.scope.meeting_id)
                or LineageSource(coverage.scope, "missing", None, None),
            )
            for coverage in register.meetings
            if same_twin(coverage.scope, target.scope)
        ),
    )


def load_record_lineage(
    scopes: Sequence[MeetingScope],
    record_path_for: Callable[[str], Path],
    record_id: str,
) -> RecordLineage:
    register = build_register(scopes, record_path_for, RegisterQuery())
    target = next((item for item in register.records if item.record_id == record_id), None)
    if target is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    sources = [
        read_lineage(scope, record_path_for(scope.meeting_id))
        for scope in scopes
        if same_twin(scope, target.scope)
    ]
    return record_lineage(target, register, sources)


def _publication_outcome(
    document: LineageImport, current: LineageSource, *, meeting_id: str
) -> LineageOutcome:
    if current.revision is None:
        if document.replaces is not None:
            raise FakeYouError(
                ErrorCode.DECISION_LINEAGE_REVISION_STALE,
                meeting_id=meeting_id,
                revision="none",
                expected=document.replaces,
            )
        return "created"
    if document.replaces is None:
        raise FakeYouError(
            ErrorCode.DECISION_LINEAGE_EXISTS, meeting_id=meeting_id, revision=current.revision
        )
    if document.replaces != current.revision:
        raise FakeYouError(
            ErrorCode.DECISION_LINEAGE_REVISION_STALE,
            meeting_id=meeting_id,
            revision=current.revision,
            expected=document.replaces,
        )
    return "replaced"


def _import_locked(
    scopes: Sequence[MeetingScope],
    record_path_for: Callable[[str], Path],
    document: LineageImport,
    *,
    owner_id: str,
    imported_at: datetime,
) -> LineagePublication:
    register = build_register(scopes, record_path_for, RegisterQuery())
    lineage = bind_lineage(document, owner_id=owner_id, register=register, imported_at=imported_at)
    scope = next(item for item in scopes if item.meeting_id == lineage.meeting_id)
    record_path = record_path_for(lineage.meeting_id)
    current = read_lineage(scope, record_path)
    if current.lineage is not None and current.revision is not None:
        if current.lineage.same_comparisons(lineage):
            return LineagePublication(current.lineage, current.revision, "unchanged")
    outcome = _publication_outcome(document, current, meeting_id=lineage.meeting_id)
    if not MeetingDir.from_record_path(record_path).dir.is_dir():
        raise FakeYouError(ErrorCode.INPUT_INVALID, reason="lineage_source_unavailable")
    return LineagePublication(lineage, publish_lineage(record_path, lineage), outcome)


def import_lineage(
    scopes: Sequence[MeetingScope],
    record_path_for: Callable[[str], Path],
    document: LineageImport,
    *,
    owner_id: str,
    imported_at: datetime,
) -> LineagePublication:
    if not any(scope.meeting_id == document.meeting_id for scope in scopes):
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    try:
        with record_lock(record_path_for(document.meeting_id), purpose="lineage"):
            return _import_locked(
                scopes,
                record_path_for,
                document,
                owner_id=owner_id,
                imported_at=imported_at,
            )
    except RecordLockError as error:
        if error.code is not ErrorCode.RECORD_LOCKED:
            raise
        raise FakeYouError(
            ErrorCode.DECISION_LINEAGE_LOCKED, meeting_id=document.meeting_id
        ) from None
