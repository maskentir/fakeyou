import asyncio
import shutil
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

from graphiti_core import Graphiti
from pydantic import TypeAdapter

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli._operator import OperatorIdentity, load_operator_persona
from fakeyou.cli.diarize import diarize_run
from fakeyou.cli.output import emit_error, emit_json, emit_progress, emit_text
from fakeyou.config.settings import (
    IngestionSettings,
    Settings,
)
from fakeyou.conversation.conversation import (
    conversation_thread_id,
)
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url
from fakeyou.foundation.invariants import SessionId
from fakeyou.memory.ingestion import (
    IngestControl,
    IngestResult,
    IngestScope,
    ingest_meeting,
)
from fakeyou.memory.memory import (
    ensure_indexes,
    forget_episode,
    forget_meeting,
    open_graphiti_memory,
    reembed_group,
)
from fakeyou.memory.owner_notes import (
    NoteIdentity,
    OwnerNoteResult,
)
from fakeyou.memory.owner_notes import (
    remember_owner_note as remember_owner_note,
)
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal, JournalFile, journal_files, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.orchestration.providers import memory_config, open_persona_memory
from fakeyou.persistence.ingestion_status import (
    IngestionState,
    IngestionStatus,
    hash_meeting_record,
    read_status,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.record import load_meeting_record, meeting_record_path


@dataclass(frozen=True, slots=True)
class CorrectionTarget:
    episode_uuid: str
    note_id: UUID | None = None


async def initialize_memory(settings: Settings) -> None:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        await ensure_indexes(memory)
        emit_text("Graphiti indexes initialized.")


@asynccontextmanager
async def open_operator_memory(
    identity: OperatorIdentity, settings: Settings
) -> AsyncIterator[tuple[PersonaProfile, Graphiti]]:
    profile = await load_operator_persona(identity, settings)
    async with open_persona_memory(settings) as memory:
        yield profile, memory


def _require_usable_source(source_at: datetime | None) -> None:
    if source_at is None:
        return
    if source_at.tzinfo is None or source_at.utcoffset() is None:
        raise FakeYouError(ErrorCode.MEMORY_SOURCE_AT_NAIVE)
    if source_at > datetime.now(UTC):
        raise FakeYouError(ErrorCode.MEMORY_SOURCE_AT_FUTURE)


def _print_note_result(result: OwnerNoteResult) -> None:
    emit_json(
        {
            "episode_uuid": result.episode_uuid,
            "facts": list(result.facts),
            "source_at": result.source_at.isoformat(),
            "recorded_at": result.recorded_at.isoformat(),
            "supersedes": result.supersedes,
        }
    )


async def add_owner_note(
    text: str,
    *,
    identity: OperatorIdentity,
    settings: Settings,
    source_at: datetime | None = None,
    note_id: UUID | None = None,
) -> None:
    if not text.strip():
        raise FakeYouError(ErrorCode.MEMORY_NOTE_TEXT_BLANK)
    _require_usable_source(source_at)
    async with open_operator_memory(identity, settings) as (
        profile,
        memory,
    ):
        operation = note_id if note_id is not None else uuid4()
        if note_id is None:
            emit_progress(f"fakeyou: note-id {operation}")
        result = await remember_owner_note(
            memory,
            group_id=profile.memory_group,
            text=text,
            source_at=source_at,
            identity=NoteIdentity(operation),
        )
    _print_note_result(result)


async def correct_owner_note(
    text: str,
    *,
    target: CorrectionTarget,
    source_at: datetime,
    identity: OperatorIdentity,
    settings: Settings,
) -> None:
    if not text.strip():
        raise FakeYouError(ErrorCode.MEMORY_NOTE_TEXT_BLANK)
    try:
        supersedes = UUID(target.episode_uuid)
    except ValueError:
        raise FakeYouError(ErrorCode.MEMORY_EPISODE_NOT_UUID) from None
    _require_usable_source(source_at)
    async with open_operator_memory(identity, settings) as (
        profile,
        memory,
    ):
        operation = target.note_id if target.note_id is not None else uuid4()
        if target.note_id is None:
            emit_progress(f"fakeyou: note-id {operation}")
        result = await remember_owner_note(
            memory,
            group_id=profile.memory_group,
            text=text,
            source_at=source_at,
            identity=NoteIdentity(operation, supersedes),
        )
    _print_note_result(result)


@dataclass(frozen=True, slots=True)
class IngestJob:
    record_path: Path
    profile: PersonaProfile
    meeting_id: str
    twin_name: str


@dataclass(frozen=True, slots=True)
class IngestRun:
    ingestion: IngestionSettings
    stop: asyncio.Event | None = None


async def ingest_until_stopped(
    memory: Graphiti,
    job: IngestJob,
    *,
    run: IngestRun,
    journal: Journal,
    faults: FaultReporter,
) -> IngestResult:
    resolved = run.stop if run.stop is not None else asyncio.Event()
    with stop_signal_handlers(resolved):
        return await ingest_meeting(
            memory,
            load_meeting_record(job.record_path),
            IngestScope(
                job.profile.memory_group,
                job.meeting_id,
                job.twin_name,
                job.profile.vocabulary,
            ),
            IngestControl(run.ingestion, resolved),
            Diagnostics(faults=faults, journal=journal),
        )


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    state: IngestionState
    windows: int
    resumed_from: int
    ingested: int
    facts: int
    completed: bool
    record_sha256: str | None
    reason: str | None
    episode_uuids: Mapping[int, str] | None = None


def _finish_ingest(
    status_file: Path,
    outcome: IngestOutcome,
    *,
    meeting_id: str,
    group_id: str,
    faults: FaultReporter,
) -> bool:
    stored = True
    previous = read_status(status_file)
    merged = dict(previous.episode_uuids) if previous is not None else {}
    if outcome.episode_uuids:
        merged.update(outcome.episode_uuids)
    try:
        write_status(
            status_file,
            IngestionStatus(
                meeting_id=meeting_id,
                group_id=group_id,
                status=outcome.state,
                windows=outcome.windows,
                resumed_from=outcome.resumed_from,
                ingested=outcome.ingested,
                record_sha256=outcome.record_sha256,
                updated_at=datetime.now(UTC),
                reason=outcome.reason,
                episode_uuids=merged,
                corrected_at=previous.corrected_at if previous is not None else None,
                correction_episodes=list(previous.correction_episodes)
                if previous is not None
                else [],
                correction_windows=dict(previous.correction_windows)
                if previous is not None
                else {},
                correction_run=previous.correction_run if previous is not None else None,
            ),
        )
    except OSError as error:
        faults.record(classify_exception("memory_ingest", error))
        stored = False
    emit_json(
        {
            "meeting_id": meeting_id,
            "windows": outcome.windows,
            "resumed_from": outcome.resumed_from,
            "ingested": outcome.ingested,
            "facts": outcome.facts,
            "completed": outcome.completed,
            "status": outcome.state,
        }
    )
    return outcome.completed and stored


def _diarization_prereqs_met(settings: Settings, record_path: Path) -> bool:
    secret = settings.stt.api_key
    if secret is None or not secret.get_secret_value().strip():
        return False
    audio_dir = record_path.parent / "audio"
    if not audio_dir.is_dir():
        return False
    return any(audio_dir.glob("slot-*.wav"))


def _terminal_outcome(
    state: IngestionState,
    reason: str,
    fallback_sha256: str | None,
    existing: IngestionStatus | None = None,
) -> IngestOutcome:
    return IngestOutcome(
        state=state,
        windows=existing.windows if existing is not None else 0,
        resumed_from=existing.resumed_from if existing is not None else 0,
        ingested=existing.ingested if existing is not None else 0,
        facts=0,
        completed=False,
        record_sha256=existing.record_sha256 if existing is not None else fallback_sha256,
        reason=reason,
    )


def _fail_ingest(
    status_file: Path,
    outcome: IngestOutcome,
    meeting_id: str,
    group_id: str,
    faults: FaultReporter,
) -> NoReturn:
    _finish_ingest(
        status_file,
        outcome,
        meeting_id=meeting_id,
        group_id=group_id,
        faults=faults,
    )
    raise SystemExit(ExitCode.FAILURE) from None


def _maybe_diarize(
    meeting_id: str,
    settings: Settings,
    record_path: Path,
    *,
    no_correct: bool,
    faults: FaultReporter,
) -> None:
    if not no_correct and _diarization_prereqs_met(settings, record_path):
        try:
            diarize_run(meeting_id, settings=settings)
        except FakeYouError as error:
            faults.record(classify_exception("stt", error))


def _ingest_state(result: IngestResult, stop: asyncio.Event) -> tuple[IngestionState, str | None]:
    if result.completed:
        return "completed", None
    if stop.is_set():
        return "interrupted", "stop_requested"
    return "failed", "episode_failed"


async def ingest_recorded_meeting(
    *,
    meeting_id: str,
    identity: OperatorIdentity,
    twin_name: str | None,
    settings: Settings,
    no_correct: bool = False,
) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    if not record_path.exists():
        raise FakeYouError(ErrorCode.MEMORY_RECORD_MISSING, meeting_id=meeting_id, path=record_path)
    journal = Journal(
        meeting_id, file=JournalFile(journal_path(settings.paths.journal_dir, meeting_id))
    )
    faults = FaultReporter(journal=journal)
    try:
        _maybe_diarize(meeting_id, settings, record_path, no_correct=no_correct, faults=faults)
        with record_lock(record_path, purpose="ingestion"):
            status_file = status_path(record_path)
            profile = await load_operator_persona(identity, settings)
            existing = read_status(status_file)
            if existing is not None and existing.group_id != profile.memory_group:
                raise FakeYouError(ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH)
            try:
                current_hash = hash_meeting_record(record_path)
                load_meeting_record(record_path)
            except (OSError, ValueError) as error:
                faults.record(classify_exception("memory_ingest", error))
                _fail_ingest(
                    status_file,
                    _terminal_outcome("failed", "record_unreadable", None, existing),
                    meeting_id,
                    profile.memory_group,
                    faults,
                )
            if (
                existing is not None
                and existing.record_sha256 is not None
                and existing.record_sha256 != current_hash
            ):
                _fail_ingest(
                    status_file,
                    _terminal_outcome("failed", "record_changed", existing.record_sha256, existing),
                    meeting_id,
                    profile.memory_group,
                    faults,
                )
            stop = asyncio.Event()
            try:
                async with open_persona_memory(settings) as memory:
                    try:
                        result = await ingest_until_stopped(
                            memory,
                            IngestJob(
                                record_path,
                                profile,
                                meeting_id,
                                twin_name or profile.display_name,
                            ),
                            run=IngestRun(settings.memory.ingestion, stop),
                            journal=journal,
                            faults=faults,
                        )
                    except ValueError as error:
                        faults.record(classify_exception("memory_ingest", error))
                        if isinstance(error, FakeYouError):
                            emit_error(error)
                        _fail_ingest(
                            status_file,
                            _terminal_outcome("failed", "episode_name_invalid", current_hash),
                            meeting_id,
                            profile.memory_group,
                            faults,
                        )
            except ProviderFault as error:
                faults.record(error)
                _fail_ingest(
                    status_file,
                    _terminal_outcome("failed", f"provider_{error.kind}", current_hash),
                    meeting_id,
                    profile.memory_group,
                    faults,
                )
            except asyncio.CancelledError:
                _finish_ingest(
                    status_file,
                    _terminal_outcome("interrupted", "stop_requested", current_hash),
                    meeting_id=meeting_id,
                    group_id=profile.memory_group,
                    faults=faults,
                )
                raise
            state, reason = _ingest_state(result, stop)
            if not _finish_ingest(
                status_file,
                IngestOutcome(
                    state=state,
                    windows=result.windows,
                    resumed_from=result.resumed_from,
                    ingested=result.ingested,
                    facts=result.facts,
                    completed=result.completed,
                    record_sha256=current_hash,
                    reason=reason,
                    episode_uuids=result.episode_uuids,
                ),
                meeting_id=meeting_id,
                group_id=profile.memory_group,
                faults=faults,
            ):
                raise SystemExit(ExitCode.FAILURE) from None
    finally:
        journal.close()


async def _forget_meeting_memory(
    *,
    identity: OperatorIdentity,
    meeting_id: str,
    settings: Settings,
    keep_record: bool,
) -> None:
    async with open_operator_memory(identity, settings) as (
        profile,
        memory,
    ):
        removed = await forget_meeting(memory, group_id=profile.memory_group, meeting_id=meeting_id)
    summary: dict[str, Any] = {"removed_episodes": removed}
    if not keep_record:
        summary |= await forget_meeting_files(settings, profile, meeting_id)
    emit_json(summary)


async def forget_memory(
    *,
    identity: OperatorIdentity,
    meeting_id: str | None,
    episode_uuid: str | None,
    settings: Settings,
    keep_record: bool = False,
) -> None:
    if (meeting_id is None) == (episode_uuid is None):
        raise FakeYouError(ErrorCode.MEMORY_FORGET_TARGET_REQUIRED)
    if keep_record and meeting_id is None:
        raise FakeYouError(ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING)
    if meeting_id is not None:
        TypeAdapter(SessionId).validate_python(meeting_id)
        with record_lock(
            meeting_record_path(settings.paths.record_dir, meeting_id), purpose="forget"
        ):
            await _forget_meeting_memory(
                identity=identity,
                meeting_id=meeting_id,
                settings=settings,
                keep_record=keep_record,
            )
        return
    if episode_uuid is None:
        raise FakeYouError(ErrorCode.MEMORY_FORGET_TARGET_REQUIRED)
    async with open_operator_memory(identity, settings) as (
        profile,
        memory,
    ):
        await forget_episode(memory, group_id=profile.memory_group, episode_uuid=episode_uuid)
        removed = 1
    summary: dict[str, Any] = {"removed_episodes": removed}
    emit_json(summary)


async def reembed_memory(*, identity: OperatorIdentity, settings: Settings) -> None:
    async with open_operator_memory(identity, settings) as (
        profile,
        memory,
    ):
        result = await reembed_group(memory, group_id=profile.memory_group)
    emit_json(
        {
            "embedding_model": settings.memory.embedder.model,
            "embedding_dim": settings.memory.embedder.dim,
            **asdict(result),
        }
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
    url = configured_url(settings)
    thread_removed = False
    if url is not None:
        async with open_checkpointer(url) as saver:
            for thread_id in (
                conversation_thread_id(profile.owner_id, profile.twin_id, meeting_id),
                meeting_thread_id(profile.twin_id, meeting_id),
            ):
                if await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None:
                    thread_removed = True
                await saver.adelete_thread(thread_id)
    return {
        "record_removed": record_removed,
        "journal_files_removed": len(removed_journals),
        "conversation_thread_removed": thread_removed,
    }
