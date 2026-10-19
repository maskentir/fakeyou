import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.errors import DomainConflict
from fakeyou.api.overrides import stage_overrides
from fakeyou.api.schemas import (
    ForgetMeeting,
    ForgetStageView,
    ForgetView,
    IngestionView,
    JobView,
    NoteCorrect,
    NoteCreate,
    NoteView,
    SessionSnapshot,
)
from fakeyou.api.sessions import SessionStore, now_iso
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.memory.forget import (
    ForgetRequest,
    ForgetSummary,
    ForgottenFiles,
    run_forget,
)
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.owner_note_service import (
    NoteCommand,
    apply_note_for,
    forget_note_for,
    note_conflict,
)
from fakeyou.memory.owner_notes import NoteConflictError, OwnerNoteResult
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.persistence.ingestion_status import IngestionState, read_status
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.schemas import (
    INGESTION_STREAM,
    REEMBED_STREAM,
    encode_ingestion_job,
    encode_reembed_job,
)
from fakeyou.worker.documents import write_session_persona

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps

_TERMINAL_STATES = frozenset({"completed", "failed", "interrupted"})
_ENTRY_ID_PATTERN = re.compile(r"\d+-\d+")
_INGESTION_JOB_TYPE = "ingestion_job"


class JobTransport(Protocol):
    async def publish_job(self, stream: str, fields: Mapping[str, str]) -> str: ...

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...

    async def read_entry(self, stream: str, entry_id: str) -> dict[str, str] | None: ...


async def forget_meeting_for(
    access: OperatorAccess,
    *,
    meeting_id: SessionId,
    keep_record: bool,
) -> ForgetSummary:
    return await run_forget(
        ForgetRequest(meeting_id, None, keep_record),
        access=access,
        lifecycle_thread_id=meeting_thread_id,
    )


ForgetMeetings = Callable[..., Awaitable[ForgetSummary]]


@dataclass(frozen=True, slots=True)
class MemoryService:
    apply_note: Callable[[OperatorAccess, NoteCommand], Awaitable[OwnerNoteResult]] = apply_note_for
    forget_note: Callable[[OperatorAccess, str], Awaitable[None]] = forget_note_for
    forget_meeting: ForgetMeetings = forget_meeting_for


class IngestionSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    kind: Literal["ingest", "correct"]


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _deps(request: Request) -> "ApiDeps":
    return cast("ApiDeps", request.app.state.deps)


def _snapshot_store(request: Request) -> SessionStore:
    factory = _deps(request).snapshot_store
    if factory is not None:
        return cast(SessionStore, factory())
    return SessionStore(_settings(request).paths.record_dir)


def _job_transport(request: Request) -> JobTransport:
    factory = _deps(request).transport
    if factory is not None:
        return cast(JobTransport, factory())
    return cast(JobTransport, shared_stream_transport(request))


def _memory_service(request: Request) -> MemoryService:
    factory = _deps(request).memory_service
    if factory is not None:
        return cast(MemoryService, factory())
    return MemoryService()


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


def _twin_persona(request: Request, twin_id: str, owner_id: str) -> tuple[str, PersonaProfile]:
    owned = [
        (snapshot.meeting_id, snapshot.persona)
        for snapshot in _snapshot_store(request).list_for(owner_id)
        if snapshot.twin_id == twin_id and snapshot.persona is not None
    ]
    if not owned:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    return owned[-1]


def _twin_access(request: Request, twin_id: str, principal: Principal) -> OperatorAccess:
    owner_id = _owner_id(principal)
    settings = _settings(request)
    meeting_id, persona = _twin_persona(request, twin_id, owner_id)
    persona_path = write_session_persona(settings.paths.record_dir, meeting_id, persona)
    return OperatorAccess(
        OperatorIdentity(persona_path, owner_id, twin_id),
        settings,
        partial(open_persona_memory, settings),
    )


def _meeting_access(
    request: Request, snapshot: SessionSnapshot, principal: Principal
) -> OperatorAccess:
    settings = _settings(request)
    persona_path = (
        write_session_persona(settings.paths.record_dir, snapshot.meeting_id, snapshot.persona)
        if snapshot.persona is not None
        else None
    )
    return OperatorAccess(
        OperatorIdentity(persona_path, _owner_id(principal), snapshot.twin_id),
        settings,
        partial(open_persona_memory, settings),
    )


def _correction_target(episode_uuid: str) -> UUID:
    try:
        return UUID(episode_uuid)
    except ValueError:
        raise FakeYouError(ErrorCode.MEMORY_EPISODE_NOT_UUID) from None


def _note_view(result: OwnerNoteResult) -> NoteView:
    return NoteView(
        episode_uuid=result.episode_uuid,
        facts=result.facts,
        source_at=result.source_at.isoformat(),
        recorded_at=result.recorded_at.isoformat(),
        supersedes=result.supersedes,
    )


async def _apply_note(
    request: Request, access: OperatorAccess, command: NoteCommand, *, episode_uuid: str
) -> NoteView:
    try:
        result = await _memory_service(request).apply_note(access, command)
    except NoteConflictError as error:
        conflict = note_conflict(error, episode_uuid=episode_uuid)
        details = {"episode_uuid": conflict.episode_uuid, "retryable": conflict.retryable}
        raise DomainConflict(error, details) from error
    return _note_view(result)


def _persisted_state(request: Request, meeting_id: str) -> IngestionState:
    path = MeetingDir(
        _settings(request).paths.record_dir, parse_session_id(meeting_id, field="meeting_id")
    ).ingestion
    status = read_status(path)
    return "pending" if status is None else status.status


def _ingestion_view(state: IngestionState) -> IngestionView:
    return IngestionView(
        status=state, completed=state in _TERMINAL_STATES, succeeded=state == "completed"
    )


def _job_view(job_id: str, *, kind: str, meeting_id: str, state: IngestionState) -> JobView:
    return JobView(
        job_id=job_id,
        kind=kind,
        meeting_id=meeting_id,
        status=state,
        completed=state in _TERMINAL_STATES,
        succeeded=state == "completed",
    )


router = APIRouter(tags=["memory"])

_Owner = Annotated[Principal, Depends(require_owner)]


@router.post("/v1/sessions/{meeting_id}/ingestions", status_code=202, response_model=JobView)
async def submit_ingestion(
    meeting_id: str,
    payload: IngestionSubmit,
    principal: _Owner,
    request: Request,
) -> JobView:
    _owned_snapshot(request, meeting_id, principal)
    state = _persisted_state(request, meeting_id)
    await stage_overrides(_job_transport(request), meeting_id, request.headers)
    job_id = await _job_transport(request).publish_job(
        INGESTION_STREAM,
        encode_ingestion_job(meeting_id=meeting_id, kind=payload.kind, at=_now(request)),
    )
    return _job_view(job_id, kind=payload.kind, meeting_id=meeting_id, state=state)


@router.get("/v1/sessions/{meeting_id}/ingestion", response_model=IngestionView)
async def read_ingestion(
    meeting_id: str,
    principal: _Owner,
    request: Request,
) -> IngestionView:
    _owned_snapshot(request, meeting_id, principal)
    return _ingestion_view(_persisted_state(request, meeting_id))


@router.get("/v1/jobs/{job_id}", response_model=JobView)
async def read_job(
    job_id: str,
    principal: _Owner,
    request: Request,
) -> JobView:
    fields = (
        await _job_transport(request).read_entry(INGESTION_STREAM, job_id)
        if _ENTRY_ID_PATTERN.fullmatch(job_id) is not None
        else None
    )
    if fields is None or fields.get("type") != _INGESTION_JOB_TYPE:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    meeting_id = fields["meeting_id"]
    _owned_snapshot(request, meeting_id, principal)
    return _job_view(
        job_id,
        kind=fields["kind"],
        meeting_id=meeting_id,
        state=_persisted_state(request, meeting_id),
    )


@router.post("/v1/twins/{twin_id}/notes", status_code=201, response_model=NoteView)
async def add_note(
    twin_id: str,
    payload: NoteCreate,
    principal: _Owner,
    request: Request,
) -> NoteView:
    access = _twin_access(request, twin_id, principal)
    command = NoteCommand(payload.text, payload.source_at, payload.note_id)
    return await _apply_note(request, access, command, episode_uuid=str(payload.note_id))


@router.post(
    "/v1/twins/{twin_id}/notes/{episode_uuid}/corrections",
    status_code=201,
    response_model=NoteView,
)
async def correct_note(
    twin_id: str,
    episode_uuid: str,
    payload: NoteCorrect,
    principal: _Owner,
    request: Request,
) -> NoteView:
    access = _twin_access(request, twin_id, principal)
    command = NoteCommand(
        payload.text, payload.source_at, payload.note_id, _correction_target(episode_uuid)
    )
    return await _apply_note(request, access, command, episode_uuid=episode_uuid)


@router.delete("/v1/twins/{twin_id}/notes/{episode_uuid}", status_code=204)
async def forget_note(
    twin_id: str,
    episode_uuid: str,
    principal: _Owner,
    request: Request,
) -> Response:
    access = _twin_access(request, twin_id, principal)
    await _memory_service(request).forget_note(access, episode_uuid)
    return Response(status_code=204)


@router.post("/v1/sessions/{meeting_id}/forget", response_model=ForgetView)
async def forget_meeting(
    meeting_id: str,
    payload: ForgetMeeting,
    principal: _Owner,
    request: Request,
) -> ForgetView:
    _owned_snapshot(request, meeting_id, principal)
    snapshot = _owned_snapshot(request, meeting_id, principal)
    summary = await _memory_service(request).forget_meeting(
        _meeting_access(request, snapshot, principal),
        meeting_id=parse_session_id(meeting_id, field="meeting_id"),
        keep_record=payload.keep_record,
    )
    files = summary.files or ForgottenFiles(
        record_removed=False,
        journal_files_removed=0,
        conversation_thread_removed=False,
        browser_profile_removed=False,
    )
    return ForgetView(
        removed_episodes=summary.removed_episodes,
        record_removed=files.record_removed,
        journal_files_removed=files.journal_files_removed,
        conversation_thread_removed=files.conversation_thread_removed,
        browser_profile_removed=files.browser_profile_removed,
        stages=tuple(
            ForgetStageView(stage=stage.stage, status=stage.status, detail=stage.detail)
            for stage in summary.stages
        ),
        complete=summary.complete,
    )


@router.post("/v1/twins/{twin_id}/reembeddings", status_code=202, response_model=JobView)
async def submit_reembedding(
    twin_id: str,
    principal: _Owner,
    request: Request,
) -> JobView:
    owner_id = _owner_id(principal)
    _twin_persona(request, twin_id, owner_id)
    fields = encode_reembed_job(at=_now(request), owner_id=owner_id, twin_id=twin_id)
    await stage_overrides(_job_transport(request), f"twin:{twin_id}", request.headers)
    job_id = await _job_transport(request).publish_job(REEMBED_STREAM, fields)
    return JobView(
        job_id=job_id,
        kind="reembed",
        meeting_id="",
        status="queued",
        completed=False,
        succeeded=False,
    )
