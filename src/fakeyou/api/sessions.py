from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.overrides import overrides_from_headers, stage_overrides
from fakeyou.api.owner_model import owner_model
from fakeyou.api.schemas import (
    SessionCreate,
    SessionSnapshot,
    SessionView,
    build_snapshot,
)
from fakeyou.api.status import session_status, status_now
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.owner_model import OwnerModelRepository, TwinScope
from fakeyou.db.session_status import DesiredState, SessionStatus, SessionStatusRepository
from fakeyou.observability.trace import mint_trace_id, trace_id_from
from fakeyou.persistence.owner_model import FrozenOwnerModel

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CommandTransport(Protocol):
    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...


class SessionStore:
    def __init__(self, repository: SessionStatusRepository) -> None:
        self._repository = repository

    async def load(self, meeting_id: str) -> SessionSnapshot | None:
        parse_session_id(meeting_id, field="meeting_id")
        payload = await self._repository.payload(meeting_id)
        return SessionSnapshot.model_validate(payload) if payload is not None else None

    async def list_for(self, owner_id: str) -> list[SessionSnapshot]:
        snapshots = [
            SessionSnapshot.model_validate(payload)
            for payload in await self._repository.payloads_for(owner_id)
        ]
        return sorted(snapshots, key=lambda snapshot: (snapshot.created_at, snapshot.meeting_id))


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> ApiDeps:
    return cast("ApiDeps", request.app.state.deps)


async def snapshot_store(
    request: Request,
    repository: Annotated[SessionStatusRepository, Depends(session_status)],
) -> SessionStore:
    factory = _deps(request).snapshot_store
    if factory is not None:
        return cast(SessionStore, factory())
    return SessionStore(repository)


def _command_transport(request: Request) -> CommandTransport:
    factory = _deps(request).transport
    if factory is not None:
        return cast(CommandTransport, factory())
    return shared_stream_transport(request)


def _now(request: Request) -> str:
    factory = _deps(request).now_iso
    if factory is not None:
        return factory()
    return now_iso()


def _owner_id(principal: Principal) -> str:
    if principal.owner_id is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return principal.owner_id


async def _owned_session(
    repository: SessionStatusRepository,
    store: SessionStore,
    meeting_id: str,
    principal: Principal,
) -> tuple[SessionSnapshot, SessionStatus]:
    snapshot = await store.load(meeting_id)
    status = await repository.get(meeting_id)
    if snapshot is None or status is None or status.owner_id != _owner_id(principal):
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return snapshot, status


def _view(snapshot: SessionSnapshot, status: SessionStatus) -> SessionView:
    return SessionView(
        meeting_id=snapshot.meeting_id,
        phase=status.phase,
        desired_state=status.desired_state,
        snapshot_sha256=snapshot.snapshot_sha256,
        owner_id=snapshot.owner_id,
        twin_id=snapshot.twin_id,
        created_at=snapshot.created_at,
        trace_id=snapshot.trace_id,
        terminal_reason=status.terminal_reason,
        detail=status.detail,
    )


def _refuse_existing(status: SessionStatus | None, meeting_id: str) -> FakeYouError:
    if status is not None and status.terminal:
        return FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=meeting_id)
    return FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=meeting_id)


def _verify_identity(create: SessionCreate, owner_id: str) -> None:
    if create.owner_id != owner_id:
        raise FakeYouError(ErrorCode.PERSONA_IDENTITY_MISMATCH)
    if create.persona is not None and (
        create.persona.owner_id != create.owner_id or create.persona.twin_id != create.twin_id
    ):
        raise FakeYouError(ErrorCode.PERSONA_IDENTITY_MISMATCH)
    if create.brief is not None and (
        create.brief.owner_id != create.owner_id or create.brief.twin_id != create.twin_id
    ):
        raise FakeYouError(ErrorCode.MEETING_BRIEF_SCOPE_MISMATCH)


router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

_Owner = Annotated[Principal, Depends(require_owner)]
_Repository = Annotated[SessionStatusRepository, Depends(session_status)]
_OwnerModel = Annotated[OwnerModelRepository, Depends(owner_model)]
_Snapshots = Annotated[SessionStore, Depends(snapshot_store)]


@router.post("", status_code=201, response_model=SessionView)
async def start_session(
    payload: SessionCreate,
    principal: _Owner,
    request: Request,
    repository: _Repository,
    owner_models: _OwnerModel,
) -> SessionView:
    owner_id = _owner_id(principal)
    _verify_identity(payload, owner_id)
    meeting_id = (
        uuid4().hex
        if payload.meeting_id is None
        else parse_session_id(payload.meeting_id, field="meeting_id")
    )
    overrides = overrides_from_headers(request.headers)
    frozen = await owner_models.snapshot(TwinScope(owner_id, payload.twin_id))
    snapshot = build_snapshot(
        payload,
        meeting_id=meeting_id,
        at=_now(request),
        keys_supplied=bool(overrides),
        owner_model=FrozenOwnerModel.of(frozen),
    ).with_trace_id(trace_id_from(request.headers) or mint_trace_id())
    status = await repository.create(
        meeting_id, owner_id, status_now(), payload=snapshot.model_dump(mode="json")
    )
    if status is None:
        raise _refuse_existing(await repository.get(meeting_id), meeting_id)
    if overrides:
        await stage_overrides(_command_transport(request), meeting_id, request.headers)
    return _view(snapshot, status)


@router.post("/{meeting_id}/join", status_code=202, response_model=SessionView)
async def join_session(
    meeting_id: str,
    principal: _Owner,
    repository: _Repository,
    store: _Snapshots,
) -> SessionView:
    snapshot, status = await _owned_session(repository, store, meeting_id, principal)
    if status.terminal or status.desired_state == "joined":
        raise _refuse_existing(status, meeting_id)
    wanted = await _desire(repository, meeting_id, "joined")
    return _view(snapshot, wanted)


@router.post("/{meeting_id}/stop", status_code=202, response_model=SessionView)
async def stop_session(
    meeting_id: str,
    principal: _Owner,
    response: Response,
    repository: _Repository,
    store: _Snapshots,
) -> SessionView:
    snapshot, status = await _owned_session(repository, store, meeting_id, principal)
    if status.terminal:
        response.status_code = 200
        return _view(snapshot, status)
    wanted = (
        status
        if status.desired_state == "stopped"
        else await _desire(repository, meeting_id, "stopped")
    )
    return _view(snapshot, wanted)


@router.post("/{meeting_id}/resume", status_code=202, response_model=SessionView)
async def resume_session(
    meeting_id: str,
    principal: _Owner,
    repository: _Repository,
    store: _Snapshots,
) -> SessionView:
    snapshot, status = await _owned_session(repository, store, meeting_id, principal)
    if status.terminal:
        raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=meeting_id)
    if status.desired_state == "created":
        raise FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=meeting_id)
    return _view(snapshot, status)


@router.get("/{meeting_id}", response_model=SessionView)
async def read_session(
    meeting_id: str,
    principal: _Owner,
    repository: _Repository,
    store: _Snapshots,
) -> SessionView:
    return _view(*await _owned_session(repository, store, meeting_id, principal))


@router.get("", response_model=list[SessionView])
async def list_sessions(
    principal: _Owner,
    repository: _Repository,
    store: _Snapshots,
) -> list[SessionView]:
    owner_id = _owner_id(principal)
    statuses = await repository.list_for(owner_id)
    views: list[SessionView] = []
    for status in statuses:
        snapshot = await store.load(status.meeting_id)
        if snapshot is not None:
            views.append(_view(snapshot, status))
    return views


async def _desire(
    repository: SessionStatusRepository, meeting_id: str, state: DesiredState
) -> SessionStatus:
    wanted = await repository.desire(meeting_id, state, status_now())
    if wanted is None:
        raise _refuse_existing(await repository.get(meeting_id), meeting_id)
    return wanted
