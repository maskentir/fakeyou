import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager, AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, TypeGuard, cast

from graphiti_core import Graphiti
from procrastinate import RetryStrategy
from procrastinate.job_context import JobContext
from procrastinate.worker import Worker

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import is_identifier, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.engine import configured_url
from fakeyou.db.jobs import INGEST_KINDS, INGEST_QUEUE, INGEST_TASK, JobQueue
from fakeyou.db.owner_model import TwinScope, create_owner_model
from fakeyou.debrief.debrief_schema import DebriefReport
from fakeyou.debrief.proposals import publish_proposals
from fakeyou.memory.ingest_job import (
    IngestionJob,
    IngestionJobKind,
    handle_ingestion_job,
    pending_ingestion_meetings,
)
from fakeyou.memory.ingest_service import IngestPorts
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, memory_group_for
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal, journal_path
from fakeyou.persistence.ingestion_status import (
    IngestionState,
    IngestionStatus,
    RecordLockError,
    SettledJob,
    read_status,
    record_lock,
    write_status,
)
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.segments import ObjectStore, SegmentFile, push_appended_segments
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import Snapshot, SnapshotStore
from fakeyou.worker.documents import write_session_persona
from fakeyou.worker.keys import meeting_job_settings

logger = logging.getLogger(__name__)

INGESTION_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "ingestion_job_invalid"
FAILED_JOB_DETAIL = "ingestion_job_failed"
DEAD_LETTER_DETAIL = "ingestion_job_dead_lettered"
DEAD_LETTER_RETRY_SECONDS = 30.0
_FAULT_SOURCE = "ingestion"


def _fresh_status(
    meeting_id: str, status: IngestionState, snapshot: Snapshot | None
) -> IngestionStatus | None:
    if snapshot is None:
        return None
    return IngestionStatus(
        meeting_id=meeting_id,
        group_id=memory_group_for(snapshot.owner_id, snapshot.twin_id),
        status=status,
    )


class IngestionHandler(Protocol):
    async def __call__(
        self,
        job: IngestionJob,
        *,
        access: OperatorAccess,
        ports: IngestPorts,
    ) -> IngestSummary: ...


@dataclass(frozen=True, slots=True)
class IngestionDeps:
    settings: Settings
    transport: StreamTransport
    store: SnapshotStore
    open_memory: Callable[[Settings], AbstractAsyncContextManager[Graphiti]]
    shutdown: asyncio.Event
    handler: IngestionHandler = handle_ingestion_job
    journal: Journal | None = None
    objects: ObjectStore | None = None


def _without_stop_signals(_stop: asyncio.Event, _journal: Journal) -> AbstractContextManager[None]:
    return nullcontext()


def _log_summary(summary: IngestSummary) -> None:
    logger.info(
        "ingestion job for %s settled with status %s (completed=%s, succeeded=%s)",
        summary.meeting_id,
        summary.state,
        summary.completed,
        summary.succeeded,
    )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _propose_owner_model(settings: Settings, report: DebriefReport) -> int:
    if configured_url(settings) is None:
        return 0
    store = create_owner_model(settings)
    try:
        return await publish_proposals(
            store,
            TwinScope(owner_id=report.owner_id, twin_id=report.twin_id),
            report,
            at=datetime.now(UTC),
        )
    finally:
        await store.close()


def _missing_job_keys(error: BaseException) -> TypeGuard[FakeYouError]:
    return isinstance(error, FakeYouError) and error.code == ErrorCode.WORKER_JOB_KEYS_MISSING


class IngestionWorker:
    def __init__(
        self, deps: IngestionDeps, queue: JobQueue, *, retry: RetryStrategy | None = None
    ) -> None:
        self._deps = deps
        self._handler = deps.handler
        self._transport = deps.transport
        self._queue = queue
        queue.task(
            pass_context=True,
            name=INGEST_TASK,
            queue=INGEST_QUEUE,
            retry=retry
            or RetryStrategy(max_attempts=INGESTION_DELIVERY_LIMIT - 1, wait=5, exponential_wait=2),
        )(self._ingest_meeting)

    async def run_forever(self) -> None:
        await self._queue.open()
        await self.requeue_pending()
        if self._deps.shutdown.is_set():
            await self._queue.close()
            return
        worker = Worker(app=self._queue.app, queues=[INGEST_QUEUE], install_signal_handlers=False)
        run = cast(Callable[[], Coroutine[object, object, None]], worker.run)
        stop = cast(Callable[[], None], worker.stop)
        running = asyncio.create_task(run())
        try:
            await self._deps.shutdown.wait()
        finally:
            stop()
            await running
            await self._queue.close()

    async def run_once(self) -> None:
        await self._queue.open()
        await self._queue.app.run_worker_async(
            queues=[INGEST_QUEUE], wait=False, install_signal_handlers=False
        )

    async def requeue_pending(self) -> list[str]:
        queued = {
            job.args.get("meeting_id")
            for job in await self._queue.jobs_for(INGEST_TASK)
            if job.status in {"todo", "doing"}
        }
        requeued: list[str] = []
        for meeting_id in pending_ingestion_meetings(self._deps.settings.paths.record_dir):
            if meeting_id in queued:
                continue
            await self._queue.defer_ingest(meeting_id=meeting_id, kind="ingest")
            requeued.append(meeting_id)
        if requeued:
            logger.info("requeued pending ingestion for %s", ", ".join(requeued))
        return requeued

    async def _ingest_meeting(
        self, context: JobContext, meeting_id: str, *, kind: str
    ) -> IngestSummary | None:
        job_id = str(context.job.id) if context.job.id else "0"
        if not is_identifier(meeting_id) or kind not in INGEST_KINDS:
            await self._fault(meeting_id, INVALID_JOB_DETAIL)
            if is_identifier(meeting_id):
                await self._record_failure(meeting_id, job_id, INVALID_JOB_DETAIL, correction=False)
            return None
        job = IngestionJob(meeting_id=meeting_id, kind=cast(IngestionJobKind, kind), job_id=job_id)
        reported: list[FakeYouError] = []
        try:
            summary = await self._handle(job, reported)
            if not summary.stored:
                raise RuntimeError("ingestion job published an unstored summary")
        except Exception as error:
            await self._fault_reported(meeting_id, reported)
            if await self._settle_failure(job, error, context):
                return None
            raise
        finally:
            await self._push_journal_segments(meeting_id)
        await self._fault_reported(meeting_id, reported)
        return summary

    async def _fault_reported(self, meeting_id: str, reported: list[FakeYouError]) -> None:
        for item in reported:
            await self._fault(meeting_id, str(item.code))

    async def _push_journal_segments(self, meeting_id: str) -> None:
        objects = self._deps.objects
        if objects is None:
            return
        files = (
            SegmentFile(
                "journal",
                journal_path(
                    self._deps.settings.paths.journal_dir,
                    parse_session_id(meeting_id, field="meeting_id"),
                ),
            ),
        )
        _pushed, error = await capture_error(
            partial(push_appended_segments, objects, meeting_id, files)
        )
        if error is not None:
            logger.warning("pushing journal segments for meeting %s failed: %s", meeting_id, error)

    async def _settle_failure(
        self, job: IngestionJob, error: Exception, context: JobContext
    ) -> bool:
        await self._fault(job.meeting_id, FAILED_JOB_DETAIL, error_type=type(error).__name__)
        if _missing_job_keys(error):
            return await self._record_failure(
                job.meeting_id, job.job_id, str(ErrorCode.WORKER_JOB_KEYS_MISSING), correction=False
            )
        attempts = context.job.attempts
        if attempts + 1 < INGESTION_DELIVERY_LIMIT:
            return False
        await self._fault(
            job.meeting_id, DEAD_LETTER_DETAIL, deliveries=str(INGESTION_DELIVERY_LIMIT)
        )
        recorded = await self._record_failure(
            job.meeting_id, job.job_id, DEAD_LETTER_DETAIL, correction=job.kind == "correct"
        )
        if recorded:
            return False
        await self._queue.defer_ingest(
            meeting_id=job.meeting_id, kind=job.kind, delay_seconds=DEAD_LETTER_RETRY_SECONDS
        )
        return True

    async def _handle(self, job: IngestionJob, reported: list[FakeYouError]) -> IngestSummary:
        return await self._handler(
            job,
            access=await self._access(job.meeting_id),
            ports=IngestPorts(
                stop_signals=_without_stop_signals,
                publish=_log_summary,
                report=reported.append,
                propose_owner_model=partial(_propose_owner_model, self._deps.settings),
            ),
        )

    async def _access(self, meeting_id: str) -> OperatorAccess:
        settings = await meeting_job_settings(
            self._deps.transport, self._deps.store, self._deps.settings, meeting_id
        )
        snapshot = await self._deps.store.load(meeting_id)
        if snapshot is None:
            identity = OperatorIdentity(None)
        else:
            persona_path = (
                write_session_persona(settings.paths.record_dir, meeting_id, snapshot.persona)
                if snapshot.persona is not None
                else None
            )
            identity = OperatorIdentity(persona_path, snapshot.owner_id, snapshot.twin_id)
        return OperatorAccess(identity, settings, partial(self._deps.open_memory, settings))

    async def _fault(self, meeting_id: str, detail: str, **extra: str) -> None:
        logger.warning("ingestion job faulted: %s=%s %s", meeting_id, detail, extra)
        if not is_identifier(meeting_id):
            return
        await self._transport.publish_event(
            meeting_id,
            {
                "v": "1",
                "type": "fault",
                "at": _now_iso(),
                "meeting_id": meeting_id,
                "source": _FAULT_SOURCE,
                "detail": detail,
                **extra,
            },
        )

    async def _record_failure(
        self, meeting_id: str, job_id: str, reason: str, *, correction: bool
    ) -> bool:
        meeting = MeetingDir(
            self._deps.settings.paths.record_dir, parse_session_id(meeting_id, field="meeting_id")
        )
        snapshot = await self._deps.store.load(meeting_id)
        if not meeting.ingestion.exists() and snapshot is None:
            return True
        try:
            with record_lock(meeting.record, purpose="ingestion"):
                previous = read_status(meeting.ingestion) or _fresh_status(
                    meeting_id, "not_requested" if correction else "failed", snapshot
                )
                if previous is not None:
                    settled: dict[str, object] = {
                        "settled_job": SettledJob(id=job_id, outcome="failed"),
                        "updated_at": datetime.now(UTC),
                    }
                    if not correction:
                        settled |= {"status": "failed", "reason": reason}
                    write_status(meeting.ingestion, previous.model_copy(update=settled))
        except RecordLockError as error:
            if error.code is ErrorCode.RECORD_LOCKED:
                return False
            logger.warning("ingestion status for %s not recorded: %s", meeting_id, error.code)
        except (FakeYouError, OSError) as error:
            logger.warning("ingestion status for %s not recorded: %s", meeting_id, error)
        return True
