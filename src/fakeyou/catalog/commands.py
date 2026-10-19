from enum import StrEnum


class Command(StrEnum):
    DB_UPGRADE = "fakeyou db upgrade"
    DB_STAMP = "fakeyou db stamp"
    MEMORY_INGEST = "fakeyou memory-ingest"
    MEMORY_FORGET = "fakeyou memory-forget"
    COMPOSE_WORKER_SESSIONS = "docker compose up worker-sessions"
    NPM_CI = "npm ci --ignore-scripts"
