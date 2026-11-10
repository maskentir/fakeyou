from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from fakeyou.api.auth import Principal, require_owner
from fakeyou.api.overrides import stage_overrides
from fakeyou.api.schemas import (
    ForgetMeeting,
    ForgetStageView,
    ForgetView,
    IngestionView,
    JobView,
    SessionSnapshot,
)
from fakeyou.api.sessions import SessionStore, now_iso
from fakeyou.api.status import queued_jobs, require_jobs, session_status
from fakeyou.api.transport import shared_stream_transport
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.jobs import INGEST_KINDS, INGEST_TASK, REEMBED_TASK, JobQueue, QueuedJob
from fakeyou.db.session_status import SessionStatusRepository
from fakeyou.memory.forget import ForgottenFiles, run_forget
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.persistence.ingestion_status import IngestionState, IngestionStatus, read_status
from fakeyou.persistence.layout import MeetingDir
from fakeyou.worker.documents import write_session_persona

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps

_TERMINAL_STATES = frozenset({"completed", "failed", "interrupted"})


class JobTransport(Protocol):
    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None: ...


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


def _queued_job_view(job: QueuedJob) -> JobView:
    status = {
        "todo": "queued",
        "doing": "running",
        "succeeded": "completed",
        "failed": "failed",
        "aborted": "failed",
    }.get(job.status, job.status)
    return JobView(
        job_id=str(job.id),
        kind="reembed",
        meeting_id="",
        status=status,
        completed=status in {"completed", "failed"},
        succeeded=status == "completed",
    )


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


def _persisted_status(request: Request, meeting_id: str) -> IngestionStatus | None:
    path = MeetingDir(
        _settings(request).paths.record_dir, parse_session_id(meeting_id, field="meeting_id")
    ).ingestion
    return read_status(path)


def _persisted_state(request: Request, meeting_id: str) -> IngestionState:
    status = _persisted_status(request, meeting_id)
    return "pending" if status is None else status.status


def _job_state(request: Request, meeting_id: str, job_id: str) -> IngestionState:
    status = _persisted_status(request, meeting_id)
    settled = status.settled_job if status is not None else None
    return settled.outcome if settled is not None and settled.id == job_id else "pending"


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
    queue: Annotated[JobQueue | None, Depends(queued_jobs)],
) -> JobView:
    _owned_snapshot(request, meeting_id, principal)
    await stage_overrides(_job_transport(request), meeting_id, request.headers)
    job_id = await require_jobs(queue).defer_ingest(meeting_id=meeting_id, kind=payload.kind)
    return _job_view(str(job_id), kind=payload.kind, meeting_id=meeting_id, state="pending")


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
    queue: Annotated[JobQueue | None, Depends(queued_jobs)],
) -> JobView:
    if not job_id.isdigit():
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    queued = await require_jobs(queue).fetch(int(job_id))
    if queued is None:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    if queued.task == INGEST_TASK:
        meeting_id = str(queued.args.get("meeting_id", ""))
        kind = str(queued.args.get("kind", ""))
        _owned_snapshot(request, meeting_id, principal)
        if kind not in INGEST_KINDS:
            raise FakeYouError(ErrorCode.API_NOT_FOUND)
        return _job_view(
            job_id,
            kind=kind,
            meeting_id=meeting_id,
            state=_job_state(request, meeting_id, job_id),
        )
    if queued.task != REEMBED_TASK:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    owner_id = _owner_id(principal)
    if queued.args.get("owner_id") != owner_id:
        raise FakeYouError(ErrorCode.API_NOT_FOUND)
    _twin_persona(request, str(queued.args.get("twin_id", "")), owner_id)
    return _queued_job_view(queued)


@router.post("/v1/sessions/{meeting_id}/forget", response_model=ForgetView)
async def forget_meeting(
    meeting_id: str,
    payload: ForgetMeeting,
    principal: _Owner,
    request: Request,
    repository: Annotated[SessionStatusRepository, Depends(session_status)],
) -> ForgetView:
    snapshot = _owned_snapshot(request, meeting_id, principal)
    status = await repository.get(meeting_id)
    if status is not None and not status.terminal:
        raise FakeYouError(ErrorCode.MEMORY_FORGET_SESSION_LIVE, meeting_id=meeting_id)
    summary = await run_forget(
        _meeting_access(request, snapshot, principal),
        meeting_id=parse_session_id(meeting_id, field="meeting_id"),
        keep_record=payload.keep_record,
        status=repository,
    )
    files = summary.files or ForgottenFiles(
        record_removed=False,
        journal_files_removed=0,
        status_removed=False,
        browser_profile_removed=False,
    )
    return ForgetView(
        removed_episodes=summary.removed_episodes,
        record_removed=files.record_removed,
        journal_files_removed=files.journal_files_removed,
        status_removed=files.status_removed,
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
    queue: Annotated[JobQueue | None, Depends(queued_jobs)],
) -> JobView:
    owner_id = _owner_id(principal)
    _twin_persona(request, twin_id, owner_id)
    await stage_overrides(_job_transport(request), f"twin:{twin_id}", request.headers)
    job_id = await require_jobs(queue).defer_reembed(owner_id=owner_id, twin_id=twin_id)
    return JobView(
        job_id=str(job_id),
        kind="reembed",
        meeting_id="",
        status="queued",
        completed=False,
        succeeded=False,
    )
