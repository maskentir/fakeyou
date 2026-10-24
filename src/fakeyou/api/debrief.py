from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.overrides import stage_overrides
from fakeyou.api.schemas import SessionSnapshot
from fakeyou.api.sessions import SessionStore, now_iso
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import debrief_report_status, read_debrief
from fakeyou.debrief.debrief_schema import DebriefValidationError
from fakeyou.observability.journal import journal_path
from fakeyou.observability.report import build_session_report, read_journal_with_corrupt
from fakeyou.observability.scorecard import score_session
from fakeyou.persistence.record import meeting_record_path
from fakeyou.transport.schemas import DEBRIEF_STREAM

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps


class DebriefPublisher(Protocol):
    async def publish_job(self, stream: str, fields: Mapping[str, str]) -> str: ...

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...


class DebriefSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    force: bool


class DebriefQueued(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    meeting_id: str
    status: Literal["queued"]


class DebriefView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    status: Literal["available", "stale", "missing", "invalid"]
    review_status: Literal["unreviewed"]
    cached: bool
    report: dict[str, Any] | None


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> "ApiDeps":
    return cast("ApiDeps", request.app.state.deps)


def _snapshot_store(request: Request) -> SessionStore:
    factory = _deps(request).snapshot_store
    if factory is not None:
        return cast(SessionStore, factory())
    return SessionStore(_settings(request).paths.record_dir)


def _publisher(request: Request) -> DebriefPublisher:
    factory = _deps(request).transport
    if factory is not None:
        return cast(DebriefPublisher, factory())
    return cast(DebriefPublisher, shared_stream_transport(request))


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


def _served_report(record_path: Path) -> dict[str, Any] | None:
    try:
        return read_debrief(record_path).model_dump(mode="json")
    except DebriefValidationError:
        return None


def _debrief_job_fields(meeting_id: str, *, force: bool, at: str) -> dict[str, str]:
    return {
        "v": "1",
        "type": "debrief_job",
        "at": at,
        "meeting_id": meeting_id,
        "force": "true" if force else "false",
    }


router = APIRouter(prefix="/v1/sessions", tags=["debrief"])

_Owner = Annotated[Principal, Depends(require_owner)]


@router.post("/{meeting_id}/debriefs", status_code=202, response_model=DebriefQueued)
async def submit_debrief(
    meeting_id: str,
    payload: DebriefSubmit,
    principal: _Owner,
    request: Request,
) -> DebriefQueued:
    _owned_snapshot(request, meeting_id, principal)
    await stage_overrides(_publisher(request), meeting_id, request.headers)
    await _publisher(request).publish_job(
        DEBRIEF_STREAM,
        _debrief_job_fields(meeting_id, force=payload.force, at=_now(request)),
    )
    return DebriefQueued(meeting_id=meeting_id, status="queued")


@router.get("/{meeting_id}/debrief", response_model=DebriefView)
async def read_debrief_artifact(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> DebriefView:
    _owned_snapshot(request, meeting_id, principal)
    record_path = meeting_record_path(
        _settings(request).paths.record_dir,
        parse_session_id(meeting_id, field="meeting_id"),
    )
    report = _served_report(record_path)
    return DebriefView(
        status=debrief_report_status(record_path),
        review_status="unreviewed",
        cached=report is not None,
        report=report,
    )


@router.get("/{meeting_id}/report")
async def read_session_report(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> dict[str, Any]:
    _owned_snapshot(request, meeting_id, principal)
    settings = _settings(request)
    session = parse_session_id(meeting_id, field="meeting_id")
    path = journal_path(settings.paths.journal_dir, session)
    if not path.exists():
        raise FakeYouError(
            ErrorCode.DIAGNOSTICS_JOURNAL_MISSING,
            meeting_id=meeting_id,
            path=path.name,
        )
    try:
        rows, corrupt = read_journal_with_corrupt(path)
        report = build_session_report(rows, corrupt_lines=corrupt)
    except OSError as error:
        raise FakeYouError(
            ErrorCode.DIAGNOSTICS_REPORT_FAILED, reason=type(error).__name__
        ) from error
    report["debrief"] = {
        "status": debrief_report_status(meeting_record_path(settings.paths.record_dir, session))
    }
    report["scorecard"] = score_session(report)
    return {"meeting_id": meeting_id, **report}
