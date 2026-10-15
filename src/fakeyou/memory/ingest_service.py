import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from graphiti_core import Graphiti

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import IngestionSettings, Settings
from fakeyou.diarization.diarize_service import diarize_run
from fakeyou.memory.ingest_status import (
    IngestOutcome,
    IngestSummary,
    finish_ingest,
    ingest_state,
    terminal_outcome,
)
from fakeyou.memory.ingestion import IngestControl, IngestResult, IngestScope, ingest_meeting
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import PersonaProfile, load_operator_persona
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.persistence.ingestion_status import (
    hash_meeting_record,
    read_status,
    record_lock,
    status_path,
)
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.record import load_meeting_record, meeting_record_path


@dataclass(frozen=True, slots=True)
class IngestRequest:
    meeting_id: SessionId
    twin_name: str | None
    no_correct: bool = False


@dataclass(frozen=True, slots=True)
class IngestPorts:
    stop_signals: Callable[[asyncio.Event, Journal], AbstractContextManager[None]]
    publish: Callable[[IngestSummary], None]
    report: Callable[[FakeYouError], None]


@dataclass(frozen=True, slots=True)
class IngestJob:
    record_path: Path
    profile: PersonaProfile
    meeting_id: str
    twin_name: str


@dataclass(frozen=True, slots=True)
class IngestRun:
    ingestion: IngestionSettings
    stop: asyncio.Event


@dataclass(frozen=True, slots=True)
class _Attempt:
    request: IngestRequest
    record_path: Path
    status_file: Path
    profile: PersonaProfile
    journal: Journal
    faults: FaultReporter
    access: OperatorAccess
    ports: IngestPorts

    def settle(self, outcome: IngestOutcome) -> bool:
        summary = finish_ingest(
            self.status_file,
            outcome,
            meeting_id=self.request.meeting_id,
            group_id=self.profile.memory_group,
            faults=self.faults,
        )
        self.ports.publish(summary)
        return summary.succeeded


async def ingest_until_stopped(
    memory: Graphiti,
    job: IngestJob,
    *,
    run: IngestRun,
    journal: Journal,
    faults: FaultReporter,
) -> IngestResult:
    return await ingest_meeting(
        memory,
        load_meeting_record(job.record_path),
        IngestScope(
            job.profile.memory_group, job.meeting_id, job.twin_name, job.profile.vocabulary
        ),
        IngestControl(run.ingestion, run.stop),
        Diagnostics(faults=faults, journal=journal),
    )


async def run_recorded_ingest(
    request: IngestRequest, *, access: OperatorAccess, ports: IngestPorts
) -> bool:
    settings = access.settings
    record_path = meeting_record_path(settings.paths.record_dir, request.meeting_id)
    if not record_path.exists():
        raise FakeYouError(
            ErrorCode.MEMORY_RECORD_MISSING, meeting_id=request.meeting_id, path=record_path
        )
    journal = Journal(
        request.meeting_id,
        file=JournalFile(journal_path(settings.paths.journal_dir, request.meeting_id)),
    )
    try:
        return await _ingest_locked(request, record_path, journal, access=access, ports=ports)
    finally:
        journal.close()


async def _ingest_locked(
    request: IngestRequest,
    record_path: Path,
    journal: Journal,
    *,
    access: OperatorAccess,
    ports: IngestPorts,
) -> bool:
    faults = FaultReporter(journal=journal)
    _maybe_diarize(
        request.meeting_id,
        access.settings,
        record_path,
        no_correct=request.no_correct,
        faults=faults,
    )
    with record_lock(record_path, purpose="ingestion"):
        status_file = status_path(record_path)
        profile = await load_operator_persona(access.identity, access.settings)
        existing = read_status(status_file, journal=journal)
        if existing is not None and existing.group_id != profile.memory_group:
            raise FakeYouError(ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH)
        attempt = _Attempt(
            request, record_path, status_file, profile, journal, faults, access, ports
        )
        current_hash = _record_hash(record_path, faults)
        if current_hash is None:
            return attempt.settle(terminal_outcome("failed", "record_unreadable", None, existing))
        if (
            existing is not None
            and existing.record_sha256 is not None
            and existing.record_sha256 != current_hash
        ):
            return attempt.settle(
                terminal_outcome("failed", "record_changed", existing.record_sha256, existing)
            )
        return await _ingest_with_memory(attempt, current_hash)


def _record_hash(record_path: Path, faults: FaultReporter) -> str | None:
    try:
        current = hash_meeting_record(record_path)
        load_meeting_record(record_path)
    except (OSError, ValueError) as error:
        faults.record(classify_exception("memory_ingest", error))
        return None
    return current


async def _ingest_with_memory(attempt: _Attempt, current_hash: str) -> bool:
    stop = asyncio.Event()
    try:
        async with attempt.access.open_memory() as memory:
            try:
                result = await _ingest_records(attempt, memory, stop)
            except ValueError as error:
                return _episode_rejected(attempt, error, current_hash)
    except ProviderFault as error:
        attempt.faults.record(error)
        return attempt.settle(terminal_outcome("failed", f"provider_{error.kind}", current_hash))
    except asyncio.CancelledError:
        attempt.settle(terminal_outcome("interrupted", "stop_requested", current_hash))
        raise
    state, reason = ingest_state(result, stop)
    return attempt.settle(
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
        )
    )


async def _ingest_records(attempt: _Attempt, memory: Graphiti, stop: asyncio.Event) -> IngestResult:
    job = IngestJob(
        attempt.record_path,
        attempt.profile,
        attempt.request.meeting_id,
        attempt.request.twin_name or attempt.profile.display_name,
    )
    run = IngestRun(attempt.access.settings.memory.ingestion, stop)
    with attempt.ports.stop_signals(stop, attempt.journal):
        return await ingest_until_stopped(
            memory,
            job,
            run=run,
            journal=attempt.journal,
            faults=attempt.faults,
        )


def _episode_rejected(attempt: _Attempt, error: ValueError, current_hash: str) -> bool:
    attempt.faults.record(classify_exception("memory_ingest", error))
    if isinstance(error, FakeYouError):
        attempt.ports.report(error)
    return attempt.settle(terminal_outcome("failed", "episode_name_invalid", current_hash))


def _maybe_diarize(
    meeting_id: SessionId,
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


def _diarization_prereqs_met(settings: Settings, record_path: Path) -> bool:
    secret = settings.stt.api_key
    if secret is None or not secret.get_secret_value().strip():
        return False
    audio_dir = MeetingDir.from_record_path(record_path).audio_dir
    if not audio_dir.is_dir():
        return False
    return any(audio_dir.glob("slot-*.wav"))
