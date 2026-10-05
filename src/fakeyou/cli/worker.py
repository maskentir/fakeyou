import dataclasses
import json
from pathlib import Path

from fakeyou.cli.errors import CliError
from fakeyou.settings import Settings
from fakeyou.worker_backend import (
    DockerWorkerBackend,
    WorkerBackend,
    WorkerError,
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
    try:
        started = _backend(settings, backend).start(
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
    except WorkerError as error:
        raise CliError(str(error)) from None
    print(json.dumps(dataclasses.asdict(started), indent=2))


def worker_status(
    meeting_id: str | None, *, settings: Settings, backend: WorkerBackend | None = None
) -> None:
    try:
        if meeting_id is None:
            states = _backend(settings, backend).status_all()
            print(json.dumps([dataclasses.asdict(state) for state in states], indent=2))
            return
        state = _backend(settings, backend).status(meeting_id)
    except WorkerError as error:
        raise CliError(str(error)) from None
    print(json.dumps(dataclasses.asdict(state), indent=2))


def worker_stop(
    meeting_id: str, *, timeout: float, settings: Settings, backend: WorkerBackend | None = None
) -> None:
    try:
        stopped = _backend(settings, backend).stop(meeting_id, timeout_seconds=timeout)
    except WorkerError as error:
        raise CliError(str(error)) from None
    print(json.dumps(dataclasses.asdict(stopped), indent=2))


def worker_ingest(
    meeting_id: str,
    *,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    settings: Settings,
    backend: WorkerBackend | None = None,
) -> None:
    try:
        ingested = _backend(settings, backend).ingest(
            meeting_id, persona_path=persona_path, owner_id=owner_id, twin_id=twin_id
        )
    except WorkerError as error:
        raise CliError(str(error)) from None
    print(json.dumps(dataclasses.asdict(ingested), indent=2))
    if ingested.status != "completed":
        raise SystemExit(1)
