import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_error, emit_json, emit_progress, emit_text
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.forget import ForgetRequest, ForgetSummary, run_forget
from fakeyou.memory.ingest_service import IngestPorts, IngestRequest, run_recorded_ingest
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.maintenance import initialize_indexes, run_reembed
from fakeyou.memory.memory import open_graphiti_memory
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.owner_note_service import NoteRequest, add_note, correct_note
from fakeyou.memory.owner_notes import OwnerNoteResult
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.orchestration.providers import memory_config, open_persona_memory


@dataclass(frozen=True, slots=True)
class CorrectionTarget:
    episode_uuid: str
    note_id: UUID | None = None


def _access(identity: OperatorIdentity, settings: Settings) -> OperatorAccess:
    return OperatorAccess(identity, settings, lambda: open_persona_memory(settings))


async def initialize_memory(settings: Settings) -> None:
    await initialize_indexes(lambda: open_graphiti_memory(memory_config(settings)))
    emit_text("Graphiti indexes initialized.")


def _announce_note(operation: UUID) -> None:
    emit_progress(f"fakeyou: note-id {operation}")


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
    result = await add_note(
        NoteRequest(text, source_at, note_id),
        access=_access(identity, settings),
        announce=_announce_note,
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
    result = await correct_note(
        NoteRequest(text, source_at, target.note_id),
        supersedes=target.episode_uuid,
        access=_access(identity, settings),
        announce=_announce_note,
    )
    _print_note_result(result)


@contextmanager
def _bind_stop_signals(stop: asyncio.Event, journal: Journal) -> Iterator[None]:
    with stop_signal_handlers(stop) as handlers:
        journal.record(
            "signal_handlers_bound",
            source="cli",
            signals=list(handlers.installed),
            unavailable=list(handlers.unavailable),
        )
        yield


def _print_ingest_summary(summary: IngestSummary) -> None:
    emit_json(
        {
            "meeting_id": summary.meeting_id,
            "windows": summary.windows,
            "resumed_from": summary.resumed_from,
            "ingested": summary.ingested,
            "facts": summary.facts,
            "completed": summary.completed,
            "status": summary.state,
        }
    )


async def ingest_recorded_meeting(
    *,
    meeting_id: SessionId,
    identity: OperatorIdentity,
    twin_name: str | None,
    settings: Settings,
    no_correct: bool = False,
) -> None:
    succeeded = await run_recorded_ingest(
        IngestRequest(meeting_id, twin_name, no_correct),
        access=_access(identity, settings),
        ports=IngestPorts(
            stop_signals=_bind_stop_signals,
            publish=_print_ingest_summary,
            report=emit_error,
        ),
    )
    if not succeeded:
        raise SystemExit(ExitCode.FAILURE) from None


def _forget_document(summary: ForgetSummary) -> dict[str, Any]:
    document: dict[str, Any] = {"removed_episodes": summary.removed_episodes}
    if summary.files is not None:
        document |= asdict(summary.files)
    return document


async def forget_memory(
    *,
    identity: OperatorIdentity,
    meeting_id: SessionId | None,
    episode_uuid: str | None,
    settings: Settings,
    keep_record: bool = False,
) -> None:
    summary = await run_forget(
        ForgetRequest(meeting_id, episode_uuid, keep_record),
        access=_access(identity, settings),
        lifecycle_thread_id=meeting_thread_id,
    )
    emit_json(_forget_document(summary))


async def reembed_memory(*, identity: OperatorIdentity, settings: Settings) -> None:
    result = await run_reembed(_access(identity, settings))
    emit_json(
        {
            "embedding_model": settings.memory.embedder.model,
            "embedding_dim": settings.memory.embedder.dim,
            **asdict(result),
        }
    )
