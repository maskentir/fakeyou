import asyncio
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.sessions import SessionStore, snapshot_store
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import IDENTIFIER_BODY, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief_schema import ItemKind, ItemStatus, SourceKind
from fakeyou.debrief.register import (
    DebriefAvailability,
    DecisionRecord,
    MeetingCoverage,
    MeetingScope,
    RegisterEvidence,
    RegisterQuery,
    build_register,
    register_page,
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
    speaker_identified: bool
    at: AwareDatetime

    @classmethod
    def of(cls, evidence: RegisterEvidence) -> "DecisionEvidenceView":
        return cls(
            entry_id=evidence.entry_id,
            quote=evidence.quote,
            source_kind=evidence.source_kind,
            participant_id=evidence.participant_id,
            speaker_identified=evidence.speaker_identified,
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


router = APIRouter(prefix="/v1/decisions", tags=["decisions"])

_Owner = Annotated[Principal, Depends(require_owner)]
_Snapshots = Annotated[SessionStore, Depends(snapshot_store)]


@router.get("", response_model=DecisionRegisterView)
async def list_decisions(
    request: Request,
    principal: _Owner,
    store: _Snapshots,
    filters: Annotated[DecisionFilter, Query()],
) -> DecisionRegisterView:
    if principal.owner_id is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    settings: Settings = request.app.state.settings
    scopes = [
        MeetingScope(
            owner_id=snapshot.owner_id,
            twin_id=snapshot.twin_id,
            meeting_id=snapshot.meeting_id,
            created_at=snapshot.created_at,
        )
        for snapshot in await store.list_for(principal.owner_id)
        if snapshot.owner_id == principal.owner_id
    ]
    register = await asyncio.to_thread(
        build_register,
        scopes,
        lambda meeting: meeting_record_path(
            settings.paths.record_dir, parse_session_id(meeting, field="meeting_id")
        ),
        filters.query(),
    )
    page = register_page(register.records, cursor=filters.cursor, limit=filters.limit)
    return DecisionRegisterView(
        records=tuple(DecisionRecordView.of(record) for record in page.records),
        meetings=tuple(MeetingCoverageView.of(coverage) for coverage in register.meetings),
        next_cursor=page.next_cursor,
    )
