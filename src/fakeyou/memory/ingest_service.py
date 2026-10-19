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
from fakeyou.memory.ingestion import (
    IngestControl,
    IngestResult,
    IngestScope,
    MeetingScope,
    correction_stamp,
    ingest_corrections,
    ingest_meeting,
)
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import PersonaProfile, load_operator_persona, require_persona_group
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    finalized_prefix,
    hash_meeting_record,
    hash_record_prefix,
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
class RecordPrefix:
    sha256: str
    byte_count: int


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


def _existing_record(request: IngestRequest, settings: Settings) -> Path:
    record_path = meeting_record_path(settings.paths.record_dir, request.meeting_id)
    if not record_path.exists():
        raise FakeYouError(
            ErrorCode.MEMORY_RECORD_MISSING, meeting_id=request.meeting_id, path=record_path
        )
    return record_path


def _meeting_journal(request: IngestRequest, settings: Settings) -> Journal:
    return Journal(
        request.meeting_id,
        file=JournalFile(journal_path(settings.paths.journal_dir, request.meeting_id)),
    )


async def _locked_scope(
    record_path: Path, journal: Journal, access: OperatorAccess
) -> tuple[PersonaProfile, IngestionStatus | None]:
    profile = await load_operator_persona(access.identity, access.settings)
    existing = read_status(status_path(record_path), journal=journal)
    if existing is not None:
        require_persona_group(existing.group_id, owner_id=profile.owner_id, twin_id=profile.twin_id)
    return profile, existing


async def run_recorded_ingest(
    request: IngestRequest, *, access: OperatorAccess, ports: IngestPorts
) -> bool:
    record_path = _existing_record(request, access.settings)
    journal = _meeting_journal(request, access.settings)
    try:
        return await _ingest_locked(request, record_path, journal, access=access, ports=ports)
    finally:
        journal.close()


async def run_recorded_correction(
    request: IngestRequest, *, access: OperatorAccess, ports: IngestPorts
) -> bool:
    record_path = _existing_record(request, access.settings)
    journal = _meeting_journal(request, access.settings)
    try:
        with record_lock(record_path, purpose="ingestion"):
            profile, _existing = await _locked_scope(record_path, journal, access)
            summary = await _correct_recorded(request, record_path, profile, journal, access)
    finally:
        journal.close()
    ports.publish(summary)
    return summary.stored


async def _correct_recorded(
    request: IngestRequest,
    record_path: Path,
    profile: PersonaProfile,
    journal: Journal,
    access: OperatorAccess,
) -> IngestSummary:
    stamp = correction_stamp(record_path)
    async with access.open_memory() as memory:
        result = await ingest_corrections(
            memory,
            record_path,
            MeetingScope(profile.memory_group, request.meeting_id),
            IngestControl(access.settings.memory.ingestion),
            Diagnostics(faults=FaultReporter(journal=journal), journal=journal),
        )
    status = read_status(status_path(record_path), journal=journal)
    recorded = (stamp is not None and status is not None and status.corrected_at == stamp) or (
        result.changed_windows == 0 and result.completed
    )
    return IngestSummary(
        meeting_id=request.meeting_id,
        state=status.status if status is not None else "pending",
        windows=result.changed_windows,
        resumed_from=0,
        ingested=len(result.episodes),
        facts=0,
        completed=result.completed,
        succeeded=result.completed and recorded,
        stored=recorded,
    )


async def _ingest_locked(
    request: IngestRequest,
    record_path: Path,
    journal: Journal,
    *,
    access: OperatorAccess,
    ports: IngestPorts,
) -> bool:
    faults = FaultReporter(journal=journal)
    await asyncio.to_thread(
        _maybe_diarize,
        request.meeting_id,
        access.settings,
        record_path,
        no_correct=request.no_correct,
        faults=faults,
    )
    with record_lock(record_path, purpose="ingestion"):
        status_file = status_path(record_path)
        profile, existing = await _locked_scope(record_path, journal, access)
        attempt = _Attempt(
            request, record_path, status_file, profile, journal, faults, access, ports
        )
        prefix = _record_prefix(record_path, faults)
        if prefix is None:
            return attempt.settle(terminal_outcome("failed", "record_unreadable", None, existing))
        if existing is not None and _stored_prefix_changed(record_path, existing):
            return attempt.settle(
                terminal_outcome("failed", "record_changed", existing.record_sha256, existing)
            )
        return await _ingest_with_memory(attempt, prefix)


def _record_prefix(record_path: Path, faults: FaultReporter) -> RecordPrefix | None:
    try:
        digest, byte_count = finalized_prefix(record_path)
        load_meeting_record(record_path)
    except (OSError, ValueError) as error:
        faults.record(classify_exception("memory_ingest", error))
        return None
    return RecordPrefix(digest, byte_count)


def _stored_prefix_changed(record_path: Path, existing: IngestionStatus) -> bool:
    if existing.record_sha256 is None:
        return False
    if existing.record_bytes is None:
        return existing.record_sha256 != hash_meeting_record(record_path)
    try:
        return existing.record_sha256 != hash_record_prefix(record_path, existing.record_bytes)
    except ValueError:
        return True


async def _ingest_with_memory(attempt: _Attempt, prefix: RecordPrefix) -> bool:
    stop = asyncio.Event()
    try:
        async with attempt.access.open_memory() as memory:
            try:
                result = await _ingest_records(attempt, memory, stop)
            except ValueError as error:
                return _episode_rejected(attempt, error, prefix)
    except ProviderFault as error:
        attempt.faults.record(error)
        return attempt.settle(
            terminal_outcome(
                "failed", f"provider_{error.kind}", prefix.sha256, fallback_bytes=prefix.byte_count
            )
        )
    except asyncio.CancelledError:
        attempt.settle(
            terminal_outcome(
                "interrupted", "stop_requested", prefix.sha256, fallback_bytes=prefix.byte_count
            )
        )
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
            record_sha256=prefix.sha256,
            reason=reason,
            episode_uuids=result.episode_uuids,
            record_bytes=prefix.byte_count,
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


def _episode_rejected(attempt: _Attempt, error: ValueError, prefix: RecordPrefix) -> bool:
    attempt.faults.record(classify_exception("memory_ingest", error))
    if isinstance(error, FakeYouError):
        attempt.ports.report(error)
    return attempt.settle(
        terminal_outcome(
            "failed", "episode_name_invalid", prefix.sha256, fallback_bytes=prefix.byte_count
        )
    )


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
