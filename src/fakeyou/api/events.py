from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Annotated, Protocol, cast

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.errors import error_response
from fakeyou.api.sessions import now_iso
from fakeyou.api.status import read_status
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError, render_error
from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatus
from fakeyou.transport.schemas import validate_event

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps

_CURSOR_PATTERN = re.compile(r"\d+(?:-\d+)?")
_CURSOR_START = "0-0"
_DEFAULT_KEEPALIVE_SECONDS = 15.0
_POLL_SECONDS = 1.0
_REPLAY_COUNT = 100


class EventTransport(Protocol):
    async def read_events(
        self, meeting_id: str, *, after: str, count: int = 100
    ) -> list[tuple[str, dict[str, str]]]: ...


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> ApiDeps:
    return cast("ApiDeps", request.app.state.deps)


def _event_transport(request: Request) -> EventTransport:
    factory = _deps(request).transport
    if factory is not None:
        return cast(EventTransport, factory())
    return shared_stream_transport(request)


def _now(request: Request) -> str:
    factory = _deps(request).now_iso
    if factory is not None:
        return factory()
    return now_iso()


def _keepalive_seconds(request: Request) -> float:
    configured = _deps(request).event_keepalive_seconds
    return _DEFAULT_KEEPALIVE_SECONDS if configured is None else configured


def _owner_id(principal: Principal) -> str:
    if principal.owner_id is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return principal.owner_id


async def _owned_status(request: Request, meeting_id: str, principal: Principal) -> SessionStatus:
    status = await read_status(request, meeting_id)
    if status is None or status.owner_id != _owner_id(principal):
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return status


def _stream_id_key(entry_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = entry_id.partition("-")
    return int(milliseconds), int(sequence or "0")


def _valid_cursor(value: str) -> bool:
    return _CURSOR_PATTERN.fullmatch(value) is not None


def _event_frame(entry_id: str | None, fields: Mapping[str, str]) -> str:
    payload = json.dumps(dict(fields), ensure_ascii=True)
    prefix = "" if entry_id is None else f"id: {entry_id}\n"
    return f"{prefix}event: {fields['type']}\ndata: {payload}\n\n"


def _fault_fields(meeting_id: str, *, at: str, reason: str) -> dict[str, str]:
    return {"v": "1", "type": "fault", "at": at, "meeting_id": meeting_id, "reason": reason}


def _terminal_fields(status: SessionStatus, *, at: str) -> dict[str, str]:
    fields = {
        "v": "1",
        "type": "session_ended",
        "at": at,
        "meeting_id": status.meeting_id,
        "phase": status.phase,
    }
    if status.terminal_reason is not None:
        fields["terminal_reason"] = status.terminal_reason
    if status.detail is not None:
        fields["detail"] = status.detail
    return fields


async def _session_events(
    request: Request,
    status: SessionStatus,
    transport: EventTransport,
    *,
    after: str,
    at: str,
) -> AsyncIterator[str]:
    keepalive_seconds = _keepalive_seconds(request)
    poll_seconds = min(keepalive_seconds, _POLL_SECONDS)
    terminal = status.terminal
    cursor = after
    last_sent = time.monotonic()
    while True:
        if await request.is_disconnected():
            return
        entries = await transport.read_events(status.meeting_id, after=cursor, count=_REPLAY_COUNT)
        if entries:
            for entry_id, fields in entries:
                try:
                    validated = validate_event(fields, meeting_id=status.meeting_id)
                except ValueError as error:
                    yield _event_frame(
                        entry_id, _fault_fields(status.meeting_id, at=at, reason=str(error))
                    )
                    return
                cursor = entry_id
                yield _event_frame(entry_id, validated)
                if validated["type"] == "session_ended":
                    return
            last_sent = time.monotonic()
            continue
        if terminal:
            yield _event_frame(None, _terminal_fields(status, at=at))
            return
        await asyncio.sleep(poll_seconds)
        if time.monotonic() - last_sent >= keepalive_seconds:
            yield ": keepalive\n\n"
            last_sent = time.monotonic()


router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

_Owner = Annotated[Principal, Depends(require_owner)]


@router.get("/{meeting_id}/events")
async def stream_session_events(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> Response:
    status = await _owned_status(request, meeting_id, principal)
    cursor = request.headers.get("last-event-id")
    if cursor is not None and not _valid_cursor(cursor):
        raise FakeYouError(
            ErrorCode.INPUT_INVALID, reason="Last-Event-Id must be a stream entry ID"
        )
    after = _CURSOR_START
    if cursor is not None and cursor not in {"0", _CURSOR_START}:
        after = cursor
        oldest = await _event_transport(request).read_events(
            meeting_id, after=_CURSOR_START, count=1
        )
        if oldest and _stream_id_key(after) < _stream_id_key(oldest[0][0]):
            return error_response(
                str(ErrorCode.INPUT_INVALID),
                render_error(ErrorCode.INPUT_INVALID, reason="cursor_expired"),
                status=410,
                details={"reason": "cursor_expired"},
            )
    return StreamingResponse(
        _session_events(request, status, _event_transport(request), after=after, at=_now(request)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
