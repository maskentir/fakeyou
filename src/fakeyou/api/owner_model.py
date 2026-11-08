from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.status import status_now
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import IDENTIFIER_BODY
from fakeyou.db.owner_model import (
    Confidence,
    ItemDisclosure,
    OwnerModelEntry,
    OwnerModelKind,
    OwnerModelRepository,
    OwnerModelRevision,
    OwnerModelStore,
    RevisionSource,
    RevisionStatus,
    TwinScope,
)

_IDENTIFIER = rf"^{IDENTIFIER_BODY}$"


class OwnerModelItemBody(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True, str_strip_whitespace=True
    )

    kind: OwnerModelKind
    topic: str = Field(min_length=1, max_length=120)
    statement: str = Field(min_length=1, max_length=2000)
    reason: str | None = Field(default=None, min_length=1, max_length=2000)
    confidence: Confidence = "firm"
    disclosure: ItemDisclosure = "room_speakable"
    valid_at: AwareDatetime | None = None

    def entry(self) -> OwnerModelEntry:
        return OwnerModelEntry(
            kind=self.kind,
            topic=self.topic,
            statement=self.statement,
            confidence=self.confidence,
            disclosure=self.disclosure,
            reason=self.reason,
            valid_at=self.valid_at,
        )


class OwnerModelRevisionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision_id: str
    item_id: str
    revision: int
    kind: OwnerModelKind
    topic: str
    statement: str
    reason: str | None
    confidence: Confidence
    disclosure: ItemDisclosure
    valid_at: datetime | None
    source: RevisionSource
    status: RevisionStatus
    model_version: int | None
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def of(cls, revision: OwnerModelRevision) -> "OwnerModelRevisionView":
        entry = revision.entry
        return cls(
            revision_id=revision.revision_id,
            item_id=revision.item_id,
            revision=revision.revision,
            kind=entry.kind,
            topic=entry.topic,
            statement=entry.statement,
            reason=entry.reason,
            confidence=entry.confidence,
            disclosure=entry.disclosure,
            valid_at=entry.valid_at,
            source=revision.source,
            status=revision.status,
            model_version=revision.model_version,
            created_at=revision.created_at,
            decided_at=revision.decided_at,
        )


class OwnerModelView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    twin_id: str
    version: int
    items: list[OwnerModelRevisionView]


async def owner_model(request: Request) -> OwnerModelRepository:
    store = cast("OwnerModelStore | None", request.app.state.owner_model)
    if store is None:
        raise FakeYouError(ErrorCode.DATABASE_URL_MISSING)
    await store.require_current()
    return store


def _scope(principal: Principal, twin_id: str) -> TwinScope:
    if principal.owner_id is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return TwinScope(principal.owner_id, twin_id)


router = APIRouter(prefix="/v1/twins/{twin_id}/owner-model", tags=["owner-model"])

_Owner = Annotated[Principal, Depends(require_owner)]
_Repository = Annotated[OwnerModelRepository, Depends(owner_model)]
_TwinId = Annotated[str, Path(pattern=_IDENTIFIER)]
_ItemId = Annotated[str, Path(pattern=_IDENTIFIER)]
_RevisionId = Annotated[str, Path(pattern=_IDENTIFIER)]


@router.get("", response_model=OwnerModelView)
async def read_owner_model(
    twin_id: _TwinId,
    principal: _Owner,
    repository: _Repository,
    version: Annotated[int | None, Query(ge=0)] = None,
) -> OwnerModelView:
    snapshot = await repository.snapshot(_scope(principal, twin_id), version)
    return OwnerModelView(
        twin_id=twin_id,
        version=snapshot.version,
        items=[OwnerModelRevisionView.of(item) for item in snapshot.items],
    )


@router.post("", status_code=201, response_model=OwnerModelRevisionView)
async def add_owner_model_item(
    twin_id: _TwinId, body: OwnerModelItemBody, principal: _Owner, repository: _Repository
) -> OwnerModelRevisionView:
    scope = _scope(principal, twin_id)
    return OwnerModelRevisionView.of(
        await repository.add(scope, body.entry(), "owner", status_now())
    )


@router.get("/proposals", response_model=list[OwnerModelRevisionView])
async def list_owner_model_proposals(
    twin_id: _TwinId, principal: _Owner, repository: _Repository
) -> list[OwnerModelRevisionView]:
    proposals = await repository.proposals(_scope(principal, twin_id))
    return [OwnerModelRevisionView.of(revision) for revision in proposals]


@router.post("/proposals/{revision_id}/approve", response_model=OwnerModelRevisionView)
async def approve_owner_model_proposal(
    twin_id: _TwinId, revision_id: _RevisionId, principal: _Owner, repository: _Repository
) -> OwnerModelRevisionView:
    scope = _scope(principal, twin_id)
    decided = await repository.decide(scope, revision_id, approve=True, at=status_now())
    return OwnerModelRevisionView.of(decided)


@router.post("/proposals/{revision_id}/reject", response_model=OwnerModelRevisionView)
async def reject_owner_model_proposal(
    twin_id: _TwinId, revision_id: _RevisionId, principal: _Owner, repository: _Repository
) -> OwnerModelRevisionView:
    scope = _scope(principal, twin_id)
    decided = await repository.decide(scope, revision_id, approve=False, at=status_now())
    return OwnerModelRevisionView.of(decided)


@router.get("/items/{item_id}", response_model=list[OwnerModelRevisionView])
async def owner_model_item_history(
    twin_id: _TwinId, item_id: _ItemId, principal: _Owner, repository: _Repository
) -> list[OwnerModelRevisionView]:
    history = await repository.history(_scope(principal, twin_id), item_id)
    return [OwnerModelRevisionView.of(revision) for revision in history]


@router.post("/items/{item_id}/revisions", status_code=201, response_model=OwnerModelRevisionView)
async def revise_owner_model_item(
    twin_id: _TwinId,
    item_id: _ItemId,
    body: OwnerModelItemBody,
    principal: _Owner,
    repository: _Repository,
) -> OwnerModelRevisionView:
    scope = _scope(principal, twin_id)
    revised = await repository.revise(scope, item_id, body.entry(), "owner", status_now())
    return OwnerModelRevisionView.of(revised)


@router.post("/items/{item_id}/retire", response_model=OwnerModelRevisionView)
async def retire_owner_model_item(
    twin_id: _TwinId, item_id: _ItemId, principal: _Owner, repository: _Repository
) -> OwnerModelRevisionView:
    retired = await repository.retire(_scope(principal, twin_id), item_id, status_now())
    return OwnerModelRevisionView.of(retired)


@router.delete("/items/{item_id}", status_code=204)
async def forget_owner_model_item(
    twin_id: _TwinId, item_id: _ItemId, principal: _Owner, repository: _Repository
) -> Response:
    await repository.forget(_scope(principal, twin_id), item_id, status_now())
    return Response(status_code=204)
