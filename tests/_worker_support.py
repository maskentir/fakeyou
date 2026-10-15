from pathlib import Path
from typing import Any

from fakeyou.catalog.binaries import resolve_docker_binary
from fakeyou.worker.worker_backend import DockerWorkerBackend, WorkerStartRequest


def running_container(container_id: str, name: str) -> str:
    return f"{container_id}\trunning\t{name}\n"


def start_request(tmp_path: Path, **overrides: Any) -> WorkerStartRequest:
    persona = tmp_path / "persona.json"
    persona.write_text("{}", encoding="utf-8")
    brief = tmp_path / "brief.json"
    brief.write_text("{}", encoding="utf-8")
    values: dict[str, Any] = {
        "link": "https://meet.google.com/abc-defg-hij",
        "meeting_id": "meeting-9",
        "twin_id": "twin-1",
        "persona_path": persona,
        "brief_path": brief,
        "memory": True,
        "conversation": True,
        "audio": True,
        "duration": 600.0,
        "lobby_timeout": 60.0,
    }
    values.update(overrides)
    return WorkerStartRequest(**values)


def docker_argv(*parts: str) -> list[str]:
    docker = resolve_docker_binary()
    return [docker, *parts]


def ingest_backend(tmp_path: Path, runner: Any) -> DockerWorkerBackend:
    return DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=Path(".runtime/meetings"),
        project_dir=tmp_path,
    )
