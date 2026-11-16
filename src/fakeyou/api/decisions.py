import asyncio
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.sessions import SessionStore, now_iso, snapshot_store
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import IDENTIFIER_BODY, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief_schema import ItemKind, ItemStatus, SourceKind
from fakeyou.debrief.lineage import (
    Direction,
    LineageAccounting,
    LineageAvailability,
    LineageCoverage,
    LineageEntry,
    LineageImport,
    LineageMethod,
    LineageOutcome,
    Relationship,
    import_lineage,
    load_record_lineage,
)
from fakeyou.debrief.register import (
    Attribution,
    DebriefAvailability,
    DecisionRecord,
    MeetingCoverage,
    MeetingScope,
    RegisterEvidence,
    RegisterQuery,
    build_register,
    register_page,
    short_digest,
)
from fakeyou.persistence.record import meeting_record_path

_IDENTIFIER = rf"^{IDENTIFIER_BODY}$"
_Identifier = Annotated[str | None, Field(pattern=_IDENTIFIER, max_length=128)]


class DecisionFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    meeting_id: _Identifier = None
    twin_id: _Identifier = None
    kind: ItemKind | None = None
    status: ItemStatus | None = None
    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None

    def query(self) -> RegisterQuery:
        return RegisterQuery(
            meeting_id=self.meeting_id, twin_id=self.twin_id, kind=self.kind, status=self.status
        )


class DecisionEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entry_id: str
    quote: str
    source_kind: SourceKind
    participant_id: str
    attribution: Attribution
    at: AwareDatetime

    @classmethod
    def of(cls, evidence: RegisterEvidence) -> "DecisionEvidenceView":
        return cls(
            entry_id=evidence.entry_id,
            quote=evidence.quote,
            source_kind=evidence.source_kind,
            participant_id=evidence.participant_id,
            attribution=evidence.attribution,
            at=evidence.at,
        )


class DecisionRecordView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    owner_id: str
    twin_id: str
    meeting_id: str
    meeting_created_at: str
    kind: ItemKind
    status: ItemStatus
    text: str
    assignee_claim: str | None
    due_text: str | None
    authority: Literal["meeting_claim"]
    owner_approved: Literal[False]
    review_status: Literal["unreviewed"]
    generated_at: AwareDatetime
    evidence: tuple[DecisionEvidenceView, ...]

    @classmethod
    def of(cls, record: DecisionRecord) -> "DecisionRecordView":
        return cls(
            record_id=record.record_id,
            owner_id=record.scope.owner_id,
            twin_id=record.scope.twin_id,
            meeting_id=record.scope.meeting_id,
            meeting_created_at=record.scope.created_at,
            kind=record.kind,
            status=record.status,
            text=record.text,
            assignee_claim=record.assignee_claim,
            due_text=record.due_text,
            authority="meeting_claim",
            owner_approved=False,
            review_status="unreviewed",
            generated_at=record.generated_at,
            evidence=tuple(DecisionEvidenceView.of(item) for item in record.evidence),
        )


class MeetingCoverageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: str
    twin_id: str
    meeting_created_at: str
    debrief: DebriefAvailability
    record_count: int
    source_limitations: tuple[str, ...]

    @classmethod
    def of(cls, coverage: MeetingCoverage) -> "MeetingCoverageView":
        return cls(
            meeting_id=coverage.scope.meeting_id,
            twin_id=coverage.scope.twin_id,
            meeting_created_at=coverage.scope.created_at,
            debrief=coverage.debrief,
            record_count=coverage.record_count,
            source_limitations=coverage.source_limitations,
        )


class DecisionRegisterView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[DecisionRecordView, ...]
    meetings: tuple[MeetingCoverageView, ...]
    next_cursor: str | None


class LineageFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    limit: Annotated[int, Field(ge=1, le=200)] = 50
    cursor: Annotated[str | None, Field(min_length=1, max_length=512)] = None


class LineageComparisonView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relation_id: str
    relationship: Relationship
    direction: Direction
    earlier_record_id: str
    later_record_id: str
    other: DecisionRecordView
    method: LineageMethod
    imported_at: AwareDatetime
    authority: Literal["derived_candidate"]
    review_status: Literal["unreviewed"]
    owner_approved: Literal[False]

    @classmethod
    def of(cls, entry: LineageEntry) -> "LineageComparisonView":
        return cls(
            relation_id=entry.relation_id,
            relationship=entry.relationship,
            direction=entry.direction,
            earlier_record_id=entry.earlier.record_id,
            later_record_id=entry.later.record_id,
            other=DecisionRecordView.of(entry.other),
            method=entry.method,
            imported_at=entry.imported_at,
            authority="derived_candidate",
            review_status="unreviewed",
            owner_approved=False,
        )


class LineageAccountingView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    related_discussion: int
    potential_contradiction: int
    potential_supersession: int
    no_established_relationship: int
    unresolved: int
    invalidated: int
    not_compared: int

    @classmethod
    def of(cls, accounting: LineageAccounting) -> "LineageAccountingView":
        return cls.model_validate(accounting, from_attributes=True)


class LineageMeetingView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: str
    meeting_created_at: str
    debrief: DebriefAvailability
    lineage: LineageAvailability
    lineage_revision: str | None
    source_limitations: tuple[str, ...]

    @classmethod
    def of(cls, coverage: LineageCoverage) -> "LineageMeetingView":
        return cls(
            meeting_id=coverage.scope.meeting_id,
            meeting_created_at=coverage.scope.created_at,
            debrief=coverage.debrief,
            lineage=coverage.lineage,
            lineage_revision=coverage.lineage_revision,
            source_limitations=coverage.source_limitations,
        )


class DecisionLineageView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: DecisionRecordView
    comparisons: tuple[LineageComparisonView, ...]
    accounting: LineageAccountingView
    meetings: tuple[LineageMeetingView, ...]
    next_cursor: str | None


class LineageImported(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meeting_id: str
    twin_id: str
    method: LineageMethod
    imported_at: AwareDatetime
    comparisons: int
    revision: str
    outcome: LineageOutcome


router = APIRouter(prefix="/v1/decisions", tags=["decisions"])

_Owner = Annotated[Principal, Depends(require_owner)]
_Snapshots = Annotated[SessionStore, Depends(snapshot_store)]


def _owner_id(principal: Principal) -> str:
    if principal.owner_id is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return principal.owner_id


async def _scopes(store: SessionStore, owner_id: str) -> list[MeetingScope]:
    return [
        MeetingScope(
            owner_id=snapshot.owner_id,
            twin_id=snapshot.twin_id,
            meeting_id=snapshot.meeting_id,
            created_at=snapshot.created_at,
        )
        for snapshot in await store.list_for(owner_id)
        if snapshot.owner_id == owner_id
    ]


def _record_paths(request: Request) -> Callable[[str], Path]:
    settings: Settings = request.app.state.settings
    return lambda meeting: meeting_record_path(
        settings.paths.record_dir, parse_session_id(meeting, field="meeting_id")
    )


def _now(request: Request) -> datetime:
    factory = request.app.state.deps.now_iso
    return datetime.fromisoformat(factory() if factory is not None else now_iso())


@router.get("", response_model=DecisionRegisterView)
async def list_decisions(
    request: Request,
    principal: _Owner,
    store: _Snapshots,
    filters: Annotated[DecisionFilter, Query()],
) -> DecisionRegisterView:
    owner_id = _owner_id(principal)
    register = await asyncio.to_thread(
        build_register, await _scopes(store, owner_id), _record_paths(request), filters.query()
    )
    page = register_page(
        register.records,
        cursor=filters.cursor,
        limit=filters.limit,
        query=filters.query().fingerprint(owner_id),
    )
    return DecisionRegisterView(
        records=tuple(DecisionRecordView.of(record) for record in page.records),
        meetings=tuple(MeetingCoverageView.of(coverage) for coverage in register.meetings),
        next_cursor=page.next_cursor,
    )


@router.get("/{record_id}/lineage", response_model=DecisionLineageView)
async def read_decision_lineage(
    request: Request,
    principal: _Owner,
    store: _Snapshots,
    record_id: str,
    filters: Annotated[LineageFilter, Query()],
) -> DecisionLineageView:
    owner_id = _owner_id(principal)
    lineage = await asyncio.to_thread(
        load_record_lineage, await _scopes(store, owner_id), _record_paths(request), record_id
    )
    page = register_page(
        lineage.entries,
        cursor=filters.cursor,
        limit=filters.limit,
        query=short_digest(["lineage", owner_id, record_id]),
    )
    return DecisionLineageView(
        record=DecisionRecordView.of(lineage.record),
        comparisons=tuple(LineageComparisonView.of(entry) for entry in page.records),
        accounting=LineageAccountingView.of(lineage.accounting),
        meetings=tuple(LineageMeetingView.of(coverage) for coverage in lineage.meetings),
        next_cursor=page.next_cursor,
    )


@router.put("/lineage/{meeting_id}", response_model=LineageImported)
async def import_decision_lineage(
    request: Request,
    principal: _Owner,
    store: _Snapshots,
    meeting_id: str,
    document: LineageImport,
) -> LineageImported:
    owner_id = _owner_id(principal)
    if document.meeting_id != meeting_id:
        raise FakeYouError(ErrorCode.INPUT_INVALID, reason="lineage_scope_mismatch")
    publication = await asyncio.to_thread(
        import_lineage,
        await _scopes(store, owner_id),
        _record_paths(request),
        document,
        owner_id=owner_id,
        imported_at=_now(request),
    )
    lineage = publication.lineage
    return LineageImported(
        meeting_id=lineage.meeting_id,
        twin_id=lineage.twin_id,
        method=lineage.method,
        imported_at=lineage.imported_at,
        comparisons=len(lineage.comparisons),
        revision=publication.revision,
        outcome=publication.outcome,
    )
