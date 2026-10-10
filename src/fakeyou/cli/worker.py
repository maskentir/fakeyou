import dataclasses
import os
from pathlib import Path

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
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
    link: str,
    *,
    twin_name: str | None,
    owner_id: str | None,
    twin_id: str | None,
    meeting_id: str,
    lobby_timeout: float | None,
    duration: float | None,
    profile_dir: str | None,
    headless: bool | None,
    audio_enabled: bool,
    initial_speech: str | None,
    conversation_enabled: bool,
    persona_path: Path | None,
    brief_path: Path | None,
    memory: bool,
    wake_phrase: str | None,
    barge_in: bool,
    settings: Settings,
    backend: WorkerBackend | None = None,
) -> None:
    started = _start_backend(settings, backend).start(
        WorkerStartRequest(
            link=link,
            meeting_id=meeting_id,
            owner_id=owner_id,
            twin_id=twin_id,
            twin_name=twin_name,
            lobby_timeout=lobby_timeout,
            duration=duration,
            profile_dir=profile_dir,
            headless=headless,
            audio=audio_enabled,
            initial_speech=initial_speech,
            conversation=conversation_enabled,
            persona_path=persona_path,
            brief_path=brief_path,
            memory=memory,
            wake_phrase=wake_phrase,
            barge_in=barge_in,
        )
    )
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
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    settings: Settings,
    backend: WorkerBackend | None = None,
) -> None:
    ingested = _backend(settings, backend).ingest(
        meeting_id, persona_path=persona_path, owner_id=owner_id, twin_id=twin_id
    )
    emit_json(dataclasses.asdict(ingested))
    if ingested.status != "completed":
        raise SystemExit(ExitCode.FAILURE)
