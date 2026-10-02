import asyncio
import json
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from graphiti_core import Graphiti
from pydantic import TypeAdapter

from fakeyou.assembly import stop_signal_handlers
from fakeyou.checkpoint import open_checkpointer
from fakeyou.cli.errors import CliError
from fakeyou.conversation import (
    conversation_thread_id,
)
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import IngestResult, ingest_meeting
from fakeyou.invariants import SessionId
from fakeyou.journal import Journal, journal_files, journal_path
from fakeyou.lifecycle import meeting_thread_id
from fakeyou.memory import (
    ensure_indexes,
    forget_episode,
    forget_meeting,
    open_graphiti_memory,
    reembed_group,
)
from fakeyou.owner_notes import NoteConflictError, OwnerNoteResult, remember_owner_note
from fakeyou.persona import PersonaProfile, load_persona
from fakeyou.providers import memory_config, open_persona_memory
from fakeyou.record import load_meeting_record, meeting_record_path
from fakeyou.settings import (
    Settings,
)


async def initialize_memory(settings: Settings) -> None:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        await ensure_indexes(memory)
        print("Graphiti indexes initialized.")


async def load_operator_persona(
    persona_path: Path, owner_id: str | None, twin_id: str | None, settings: Settings
) -> PersonaProfile:
    try:
        return await load_persona(
            persona_path,
            owner_id=owner_id or settings.identity.owner_id,
            twin_id=twin_id or settings.identity.twin_id,
        )
    except OSError as error:
        raise CliError(f"--persona cannot be read: {error.strerror}") from None


@asynccontextmanager
async def open_operator_memory(
    persona_path: Path, owner_id: str | None, twin_id: str | None, settings: Settings
) -> AsyncIterator[tuple[PersonaProfile, Graphiti]]:
    profile = await load_operator_persona(persona_path, owner_id, twin_id, settings)
    async with open_persona_memory(settings) as memory:
        yield profile, memory


def _require_usable_source(source_at: datetime | None) -> None:
    if source_at is None:
        return
    if source_at.tzinfo is None or source_at.utcoffset() is None:
        raise CliError("--source-at must carry a timezone")
    if source_at > datetime.now(UTC):
        raise CliError("--source-at must not be in the future")


def _print_note_result(result: OwnerNoteResult) -> None:
    print(
        json.dumps(
            {
                "episode_uuid": result.episode_uuid,
                "facts": list(result.facts),
                "source_at": result.source_at.isoformat(),
                "recorded_at": result.recorded_at.isoformat(),
                "supersedes": result.supersedes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


async def add_owner_note(
    text: str,
    *,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    settings: Settings,
    source_at: datetime | None = None,
    note_id: UUID | None = None,
) -> None:
    if not text.strip():
        raise CliError("note text must not be blank")
    _require_usable_source(source_at)
    async with open_operator_memory(persona_path, owner_id, twin_id, settings) as (
        profile,
        memory,
    ):
        operation = note_id if note_id is not None else uuid4()
        if note_id is None:
            print(f"fakeyou: note-id {operation}", file=sys.stderr)
        try:
            result = await remember_owner_note(
                memory,
                group_id=profile.memory_group,
                text=text,
                source_at=source_at,
                note_id=operation,
            )
        except NoteConflictError as error:
            raise CliError(str(error)) from None
    _print_note_result(result)


async def correct_owner_note(
    text: str,
    *,
    episode_uuid: str,
    source_at: datetime,
    note_id: UUID | None,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    settings: Settings,
) -> None:
    if not text.strip():
        raise CliError("note text must not be blank")
    try:
        target = UUID(episode_uuid)
    except ValueError:
        raise CliError("--episode must be a UUID") from None
    _require_usable_source(source_at)
    async with open_operator_memory(persona_path, owner_id, twin_id, settings) as (
        profile,
        memory,
    ):
        operation = note_id if note_id is not None else uuid4()
        if note_id is None:
            print(f"fakeyou: note-id {operation}", file=sys.stderr)
        try:
            result = await remember_owner_note(
                memory,
                group_id=profile.memory_group,
                text=text,
                source_at=source_at,
                note_id=operation,
                supersedes=target,
            )
        except NoteConflictError as error:
            raise CliError(str(error)) from None
    _print_note_result(result)


async def ingest_until_stopped(
    memory: Graphiti,
    record_path: Path,
    *,
    profile: PersonaProfile,
    meeting_id: str,
    twin_name: str,
    journal: Journal,
    faults: FaultReporter,
    settings: Settings,
) -> IngestResult:
    stop = asyncio.Event()
    with stop_signal_handlers(stop):
        return await ingest_meeting(
            memory,
            load_meeting_record(record_path),
            group_id=profile.memory_group,
            meeting_id=meeting_id,
            twin_name=twin_name,
            vocabulary=profile.vocabulary,
            journal=journal,
            faults=faults,
            stop=stop,
            window_utterances=settings.memory.ingestion.window_utterances,
            window_seconds=settings.memory.ingestion.window_seconds,
            episode_timeout_seconds=settings.memory.ingestion.episode_timeout_seconds,
        )


async def ingest_recorded_meeting(
    *,
    meeting_id: str,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    twin_name: str | None,
    settings: Settings,
) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    if not record_path.exists():
        raise CliError(f"no meeting record for {meeting_id} at {record_path}")
    journal = Journal(meeting_id, path=journal_path(settings.paths.journal_dir, meeting_id))
    try:
        async with open_operator_memory(persona_path, owner_id, twin_id, settings) as (
            profile,
            memory,
        ):
            result = await ingest_until_stopped(
                memory,
                record_path,
                profile=profile,
                meeting_id=meeting_id,
                twin_name=twin_name or profile.display_name,
                journal=journal,
                faults=FaultReporter(journal=journal),
                settings=settings,
            )
    finally:
        journal.close()
    print(json.dumps({"meeting_id": meeting_id, **asdict(result)}))
    if not result.completed:
        raise SystemExit(1)


async def forget_memory(
    *,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    meeting_id: str | None,
    episode_uuid: str | None,
    settings: Settings,
    keep_record: bool = False,
) -> None:
    if (meeting_id is None) == (episode_uuid is None):
        raise CliError("memory-forget requires exactly one of --meeting or --episode")
    if keep_record and meeting_id is None:
        raise CliError("--keep-record only applies to --meeting")
    if meeting_id is not None:
        TypeAdapter(SessionId).validate_python(meeting_id)
    async with open_operator_memory(persona_path, owner_id, twin_id, settings) as (
        profile,
        memory,
    ):
        if meeting_id is not None:
            removed = await forget_meeting(
                memory, group_id=profile.memory_group, meeting_id=meeting_id
            )
        elif episode_uuid is not None:
            await forget_episode(memory, group_id=profile.memory_group, episode_uuid=episode_uuid)
            removed = 1
    summary: dict[str, Any] = {"removed_episodes": removed}
    if meeting_id is not None and not keep_record:
        summary |= await forget_meeting_files(settings, profile, meeting_id)
    print(json.dumps(summary))


async def reembed_memory(
    *, persona_path: Path, owner_id: str | None, twin_id: str | None, settings: Settings
) -> None:
    async with open_operator_memory(persona_path, owner_id, twin_id, settings) as (
        profile,
        memory,
    ):
        result = await reembed_group(memory, group_id=profile.memory_group)
    print(
        json.dumps(
            {
                "embedding_model": settings.memory.embedder.model,
                "embedding_dim": settings.memory.embedder.dim,
                **asdict(result),
            }
        )
    )


async def forget_meeting_files(
    settings: Settings, profile: PersonaProfile, meeting_id: str
) -> dict[str, Any]:
    record_dir = meeting_record_path(settings.paths.record_dir, meeting_id).parent
    record_removed = record_dir.exists()
    shutil.rmtree(record_dir, ignore_errors=True)
    removed_journals = [
        path for path in journal_files(settings.paths.journal_dir, meeting_id) if path.exists()
    ]
    for path in removed_journals:
        path.unlink()
    thread_removed = settings.paths.checkpoint_path.exists()
    if thread_removed:
        async with open_checkpointer(settings.paths.checkpoint_path) as saver:
            await saver.adelete_thread(
                conversation_thread_id(profile.owner_id, profile.twin_id, meeting_id)
            )
            await saver.adelete_thread(meeting_thread_id(profile.twin_id, meeting_id))
    return {
        "record_removed": record_removed,
        "journal_files_removed": len(removed_journals),
        "conversation_thread_removed": thread_removed,
    }
