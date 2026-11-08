import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from fakeyou.memory.ingestion import IngestResult
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import classify_exception
from fakeyou.persistence.ingestion_status import (
    IngestionState,
    IngestionStatus,
    RunOutcome,
    SettledJob,
    read_status,
    write_status,
)


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    state: RunOutcome
    windows: int
    resumed_from: int
    ingested: int
    facts: int
    completed: bool
    record_sha256: str | None
    reason: str | None
    episode_uuids: Mapping[int, str] | None = None
    record_bytes: int | None = None
    job_id: str | None = None


@dataclass(frozen=True, slots=True)
class IngestSummary:
    meeting_id: str
    state: IngestionState
    windows: int
    resumed_from: int
    ingested: int
    facts: int
    completed: bool
    succeeded: bool
    stored: bool


def terminal_outcome(
    state: RunOutcome,
    reason: str,
    fallback_sha256: str | None,
    existing: IngestionStatus | None = None,
    *,
    fallback_bytes: int | None = None,
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
        record_bytes=existing.record_bytes if existing is not None else fallback_bytes,
    )


def ingest_state(result: IngestResult, stop: asyncio.Event) -> tuple[RunOutcome, str | None]:
    if result.completed:
        return "completed", None
    if stop.is_set():
        return "interrupted", "stop_requested"
    return "failed", "episode_failed"


def finish_ingest(
    status_file: Path,
    outcome: IngestOutcome,
    *,
    meeting_id: str,
    group_id: str,
    faults: FaultReporter,
) -> IngestSummary:
    previous = read_status(status_file, journal=faults.journal)
    stored = True
    try:
        write_status(
            status_file,
            _next_status(previous, outcome, meeting_id=meeting_id, group_id=group_id),
        )
    except OSError as error:
        faults.record(classify_exception("memory_ingest", error))
        stored = False
    return IngestSummary(
        meeting_id=meeting_id,
        state=outcome.state,
        windows=outcome.windows,
        resumed_from=outcome.resumed_from,
        ingested=outcome.ingested,
        facts=outcome.facts,
        completed=outcome.completed,
        succeeded=outcome.completed and stored,
        stored=stored,
    )


def settle_correction(
    status_file: Path,
    summary: IngestSummary,
    *,
    job_id: str | None,
    group_id: str,
    faults: FaultReporter,
) -> IngestSummary:
    if job_id is None or not summary.stored:
        return summary
    previous = read_status(status_file, journal=faults.journal) or IngestionStatus(
        meeting_id=summary.meeting_id, group_id=group_id, status="not_requested"
    )
    settled = previous.model_copy(
        update={
            "settled_job": SettledJob(
                id=job_id, outcome="completed" if summary.succeeded else "failed"
            ),
            "updated_at": datetime.now(UTC),
        }
    )
    try:
        write_status(status_file, settled)
    except OSError as error:
        faults.record(classify_exception("memory_ingest", error))
        return replace(summary, succeeded=False, stored=False)
    return replace(summary, state=settled.status)


def _next_status(
    previous: IngestionStatus | None,
    outcome: IngestOutcome,
    *,
    meeting_id: str,
    group_id: str,
) -> IngestionStatus:
    episode_uuids = dict(previous.episode_uuids) if previous is not None else {}
    if outcome.episode_uuids:
        episode_uuids.update(outcome.episode_uuids)
    return IngestionStatus(
        meeting_id=meeting_id,
        group_id=group_id,
        status=outcome.state,
        windows=outcome.windows,
        resumed_from=outcome.resumed_from,
        ingested=outcome.ingested,
        record_sha256=outcome.record_sha256,
        record_bytes=outcome.record_bytes,
        updated_at=datetime.now(UTC),
        reason=outcome.reason,
        episode_uuids=episode_uuids,
        corrected_at=previous.corrected_at if previous is not None else None,
        correction_episodes=list(previous.correction_episodes) if previous is not None else [],
        correction_windows=dict(previous.correction_windows) if previous is not None else {},
        correction_run=previous.correction_run if previous is not None else None,
        settled_job=_settled_job(previous, outcome),
    )


def _settled_job(previous: IngestionStatus | None, outcome: IngestOutcome) -> SettledJob | None:
    if outcome.job_id is None or outcome.state == "interrupted":
        return previous.settled_job if previous is not None else None
    return SettledJob(id=outcome.job_id, outcome=outcome.state)
