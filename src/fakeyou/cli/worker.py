import dataclasses
import os

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli._operator import OperatorIdentity
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.worker.worker_backend import (
    DockerWorkerBackend,
    WorkerBackend,
    WorkerStartRequest,
)


def _backend(settings: Settings, backend: WorkerBackend | None) -> WorkerBackend:
    return (
        backend
        if backend is not None
        else DockerWorkerBackend(
            journal_dir=settings.paths.journal_dir, record_dir=settings.paths.record_dir
        )
    )


def _start_backend(settings: Settings, backend: WorkerBackend | None) -> WorkerBackend:
    if backend is None and not os.environ.get(ConfigKey.COMPOSE_WORKER_DATABASE_URL, "").strip():
        raise FakeYouError(ErrorCode.CONFIG_WORKER_DATABASE_URL_MISSING)
    return _backend(settings, backend)


def worker_start(
    request: WorkerStartRequest,
    *,
    settings: Settings,
    backend: WorkerBackend | None = None,
) -> None:
    started = _start_backend(settings, backend).start(request)
    emit_json(dataclasses.asdict(started))


def worker_status(
    meeting_id: str | None, *, settings: Settings, backend: WorkerBackend | None = None
) -> None:
    if meeting_id is None:
        states = _backend(settings, backend).status_all()
        emit_json([dataclasses.asdict(state) for state in states])
        return
    state = _backend(settings, backend).status(meeting_id)
    emit_json(dataclasses.asdict(state))


def worker_stop(
    meeting_id: str, *, timeout: float, settings: Settings, backend: WorkerBackend | None = None
) -> None:
    stopped = _backend(settings, backend).stop(meeting_id, timeout_seconds=timeout)
    emit_json(dataclasses.asdict(stopped))


def worker_ingest(
    meeting_id: str,
    *,
    identity: OperatorIdentity,
    settings: Settings,
    backend: WorkerBackend | None = None,
) -> None:
    if identity.persona_path is None:
        raise FakeYouError(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason="persona path is missing")
    ingested = _backend(settings, backend).ingest(
        meeting_id,
        persona_path=identity.persona_path,
        owner_id=identity.owner_id,
        twin_id=identity.twin_id,
    )
    emit_json(dataclasses.asdict(ingested))
    if ingested.status != "completed":
        raise SystemExit(ExitCode.FAILURE)
