from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol, cast
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, Response
from pydantic import ValidationError

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.overrides import overrides_from_headers, stage_overrides
from fakeyou.api.schemas import (
    SessionCreate,
    SessionSnapshot,
    SessionView,
    build_snapshot,
)
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.foundation.digest import canonical_json_digest
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.schemas import encode_command

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps

SESSION_FILE_NAME = "session.json"
TERMINAL_PHASES = frozenset({"ended", "failed"})


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CommandTransport(Protocol):
    async def publish_command(self, meeting_id: str, fields: Mapping[str, str]) -> str: ...

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...


def _encode(snapshot: SessionSnapshot) -> bytes:
    payload = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    return (payload + "\n").encode("utf-8")


def _payload_digest(snapshot: SessionSnapshot) -> str:
    payload = snapshot.model_dump(mode="json")
    payload.pop("snapshot_sha256")
    payload.pop("phase")
    payload.pop("terminal_reason")
    return canonical_json_digest(payload)


class SessionStore:
    def __init__(self, record_dir: Path) -> None:
        self._record_dir = record_dir

    def _session_path(self, meeting_id: str) -> Path:
        parsed = parse_session_id(meeting_id, field="meeting_id")
        return MeetingDir(self._record_dir, parsed).dir / SESSION_FILE_NAME

    def save_new(self, snapshot: SessionSnapshot) -> None:
        path = self._session_path(snapshot.meeting_id)
        data = _encode(snapshot)
        path.parent.mkdir(parents=True, exist_ok=True)
        reservation = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(reservation)
        try:
            write_atomically(path, data)
        except OSError:
            path.unlink(missing_ok=True)
            raise

    def load(self, meeting_id: str) -> SessionSnapshot | None:
        path = self._session_path(meeting_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        return SessionSnapshot.model_validate_json(raw)

    def list_for(self, owner_id: str) -> list[SessionSnapshot]:
        if not self._record_dir.is_dir():
            return []
        snapshots: list[SessionSnapshot] = []
        for entry in sorted(self._record_dir.iterdir()):
            session_file = entry / SESSION_FILE_NAME
            if not entry.is_dir() or not session_file.is_file():
                continue
            try:
                snapshot = SessionSnapshot.model_validate_json(session_file.read_bytes())
            except ValidationError:
                continue
            if snapshot.owner_id == owner_id:
                snapshots.append(snapshot)
        return sorted(snapshots, key=lambda snapshot: (snapshot.created_at, snapshot.meeting_id))

    def set_phase(
        self, meeting_id: str, *, phase: str, terminal_reason: str | None
    ) -> SessionSnapshot:
        path = self._session_path(meeting_id)
        current = self.load(meeting_id)
        if current is None:
            raise FakeYouError(ErrorCode.API_NOT_FOUND)
        if _payload_digest(current) != current.snapshot_sha256:
            raise ValueError("session snapshot payload does not match its digest")
        updated = SessionSnapshot.model_validate(
            {**current.model_dump(), "phase": phase, "terminal_reason": terminal_reason}
        )
        write_atomically(path, _encode(updated))
        return updated


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> ApiDeps:
    return cast("ApiDeps", request.app.state.deps)


def _snapshot_store(request: Request) -> SessionStore:
    factory = _deps(request).snapshot_store
    if factory is not None:
        return cast(SessionStore, factory())
    return SessionStore(_settings(request).paths.record_dir)


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


def _owned_snapshot(request: Request, meeting_id: str, principal: Principal) -> SessionSnapshot:
    snapshot = _snapshot_store(request).load(meeting_id)
    if snapshot is None or snapshot.owner_id != _owner_id(principal):
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return snapshot


def _view(snapshot: SessionSnapshot) -> SessionView:
    return SessionView(
        meeting_id=snapshot.meeting_id,
        phase=snapshot.phase,
        snapshot_sha256=snapshot.snapshot_sha256,
        owner_id=snapshot.owner_id,
        twin_id=snapshot.twin_id,
        created_at=snapshot.created_at,
        terminal_reason=snapshot.terminal_reason,
    )


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


@router.post("", status_code=201, response_model=SessionView)
async def start_session(
    payload: SessionCreate,
    principal: _Owner,
    request: Request,
) -> SessionView:
    _verify_identity(payload, _owner_id(principal))
    meeting_id = (
        uuid4().hex
        if payload.meeting_id is None
        else parse_session_id(payload.meeting_id, field="meeting_id")
    )
    overrides = overrides_from_headers(request.headers)
    snapshot = build_snapshot(
        payload, meeting_id=meeting_id, at=_now(request), keys_supplied=bool(overrides)
    )
    store = _snapshot_store(request)
    try:
        store.save_new(snapshot)
    except FileExistsError:
        existing = store.load(meeting_id)
        if existing is not None and existing.phase in TERMINAL_PHASES:
            raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=meeting_id) from None
        raise FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=meeting_id) from None
    await stage_overrides(_command_transport(request), meeting_id, request.headers)
    return _view(snapshot)


@router.post("/{meeting_id}/join", status_code=202, response_model=SessionView)
async def join_session(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> SessionView:
    snapshot = _owned_snapshot(request, meeting_id, principal)
    if snapshot.phase in TERMINAL_PHASES:
        raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=meeting_id)
    if snapshot.phase != "created":
        raise FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=meeting_id)
    updated = _snapshot_store(request).set_phase(meeting_id, phase="joining", terminal_reason=None)
    await _command_transport(request).publish_command(
        meeting_id, encode_command("join", meeting_id=meeting_id, at=_now(request))
    )
    return _view(updated)


@router.post("/{meeting_id}/stop", status_code=202, response_model=SessionView)
async def stop_session(
    meeting_id: str,
    principal: _Owner,
    request: Request,
    response: Response,
) -> SessionView:
    snapshot = _owned_snapshot(request, meeting_id, principal)
    if snapshot.phase in TERMINAL_PHASES:
        response.status_code = 200
        return _view(snapshot)
    await _command_transport(request).publish_command(
        meeting_id, encode_command("stop", meeting_id=meeting_id, at=_now(request))
    )
    return _view(snapshot)


@router.post("/{meeting_id}/resume", status_code=202, response_model=SessionView)
async def resume_session(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> SessionView:
    snapshot = _owned_snapshot(request, meeting_id, principal)
    if snapshot.phase in TERMINAL_PHASES:
        raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=meeting_id)
    if snapshot.phase == "created":
        raise FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=meeting_id)
    await _command_transport(request).publish_command(
        meeting_id, encode_command("resume", meeting_id=meeting_id, at=_now(request))
    )
    return _view(snapshot)


@router.get("/{meeting_id}", response_model=SessionView)
async def read_session(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> SessionView:
    return _view(_owned_snapshot(request, meeting_id, principal))


@router.get("", response_model=list[SessionView])
async def list_sessions(
    principal: _Owner,
    request: Request,
) -> list[SessionView]:
    owner_id = _owner_id(principal)
    return [_view(snapshot) for snapshot in _snapshot_store(request).list_for(owner_id)]
