import shlex
from enum import StrEnum
from pathlib import Path


class Command(StrEnum):
    DB_UPGRADE = "fakeyou db upgrade"
    DB_STAMP = "fakeyou db stamp"
    MEMORY_INGEST = "fakeyou memory-ingest"
    MEMORY_FORGET = "fakeyou memory-forget"
    COMPOSE_RUN_WORKER = "docker compose run meeting-worker"
    NPM_CI = "npm ci --ignore-scripts"


def memory_ingest_args(
    *,
    meeting_id: str,
    persona: str | Path,
    owner_id: str | None = None,
    twin_id: str | None = None,
) -> list[str]:
    args = [
        *shlex.split(Command.MEMORY_INGEST),
        "--meeting-id",
        meeting_id,
        "--persona",
        str(persona),
    ]
    if owner_id is not None:
        args += ["--owner-id", owner_id]
    if twin_id is not None:
        args += ["--twin-id", twin_id]
    return args
