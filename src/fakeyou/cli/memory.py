import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
from pydantic import ValidationError

from fakeyou.catalog.errors import ErrorCode
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.client import ClientSettings, admin_client, api_failure, owner_client
from fakeyou.cli.output import emit_json, emit_progress, emit_text
from fakeyou.config.identifiers import parse_session_id
from fakeyou.memory.persona import PersonaProfile

JOB_POLL_INTERVAL_SECONDS = 2.0
JOB_TIMEOUT_SECONDS = 1800.0


def twin_id_for(persona_path: Path | None, twin_id: str | None) -> str:
    if twin_id is not None:
        return str(parse_session_id(twin_id, field="--twin-id"))
    if persona_path is None:
        raise api_failure(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason="persona path is missing")
    try:
        profile = PersonaProfile.model_validate_json(persona_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        raise api_failure(
            ErrorCode.MEMORY_PERSONA_UNREADABLE, reason=type(error).__name__
        ) from None
    return str(profile.twin_id)


async def initialize_memory(
    settings: ClientSettings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> None:
    async with admin_client(settings, transport=transport) as client:
        await client.memory_init()
    emit_text("Graphiti indexes initialized.")


def _operation_id(note_id: UUID | None) -> UUID:
    if note_id is not None:
        return note_id
    generated = uuid4()
    emit_progress(f"fakeyou: note-id {generated}")
    return generated


@dataclass(frozen=True, slots=True)
class NoteDraft:
    text: str
    twin_id: str
    source_at: datetime | None = None
    note_id: UUID | None = None


async def add_owner_note(
    draft: NoteDraft,
    *,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    operation = _operation_id(draft.note_id)
    async with owner_client(settings, transport=transport) as client:
        view = await client.add_note(
            draft.twin_id,
            note_id=str(operation),
            text=draft.text,
            source_at=draft.source_at.isoformat() if draft.source_at is not None else None,
        )
    emit_json(view)


async def correct_owner_note(
    draft: NoteDraft,
    *,
    episode_uuid: str,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    if draft.source_at is None:
        raise api_failure(ErrorCode.MEMORY_SOURCE_AT_INVALID, value="missing")
    operation = _operation_id(draft.note_id)
    async with owner_client(settings, transport=transport) as client:
        view = await client.correct_note(
            draft.twin_id,
            episode_uuid,
            note_id=str(operation),
            text=draft.text,
            source_at=draft.source_at.isoformat(),
        )
    emit_json(view)


async def forget_episode_memory(
    *,
    twin_id: str,
    episode_uuid: str,
    settings: ClientSettings,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        await client.forget_episode(twin_id, episode_uuid)
    emit_json({"removed_episodes": 1})


async def forget_meeting_memory(
    *,
    meeting_id: str,
    settings: ClientSettings,
    keep_record: bool = False,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        document = await client.forget_meeting(meeting_id, keep_record=keep_record)
    emit_json(document)
    if not document["complete"]:
        failed = [stage for stage in document["stages"] if stage["status"] == "failed"]
        detail = failed[0]["detail"] if failed else "a forget stage did not settle"
        raise api_failure(ErrorCode.MEMORY_FORGET_FILES_INCOMPLETE, path=meeting_id, reason=detail)


async def _wait_for_job(
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    kind: str,
    job_id: str,
    interval: float,
    limit_seconds: float,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_seconds
    while True:
        view = await fetch()
        if view["completed"]:
            if not view["succeeded"]:
                raise api_failure(ErrorCode.API_JOB_FAILED, kind=kind, job_id=job_id)
            return view
        if loop.time() >= deadline:
            raise api_failure(
                ErrorCode.API_JOB_TIMEOUT, kind=kind, job_id=job_id, seconds=int(limit_seconds)
            )
        await asyncio.sleep(interval)


async def reembed_memory(
    *,
    twin_id: str,
    settings: ClientSettings,
    interval: float = JOB_POLL_INTERVAL_SECONDS,
    limit_seconds: float = JOB_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        job_id = (await client.submit_reembed(twin_id))["job_id"]
        view = await _wait_for_job(
            lambda: client.get_job(job_id),
            kind="reembed",
            job_id=job_id,
            interval=interval,
            limit_seconds=limit_seconds,
        )
    emit_json(view)


async def ingest_recorded_meeting(
    *,
    meeting_id: str,
    settings: ClientSettings,
    interval: float = JOB_POLL_INTERVAL_SECONDS,
    limit_seconds: float = JOB_TIMEOUT_SECONDS,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    async with owner_client(settings, transport=transport) as client:
        job_id = (await client.submit_ingestion(meeting_id, kind="ingest"))["job_id"]
        view = await _wait_for_job(
            lambda: client.ingestion_status(meeting_id),
            kind="ingest",
            job_id=job_id,
            interval=interval,
            limit_seconds=limit_seconds,
        )
    emit_json(view)
    if not view["succeeded"]:
        raise SystemExit(ExitCode.FAILURE) from None
