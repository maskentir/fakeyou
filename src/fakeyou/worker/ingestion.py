import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Protocol, TypeGuard

from graphiti_core import Graphiti

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import is_identifier, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.memory.ingest_job import (
    IngestionJob,
    handle_ingestion_job,
    parse_ingestion_job,
    pending_ingestion_meetings,
)
from fakeyou.memory.ingest_service import IngestPorts
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, memory_group_for
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import IngestionStatus, write_status
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.schemas import INGESTION_STREAM, encode_ingestion_job
from fakeyou.transport.streams import CONNECTION_ERRORS, PendingDelivery, StreamTransport
from fakeyou.worker.consumer import SnapshotStore
from fakeyou.worker.documents import write_session_persona
from fakeyou.worker.keys import meeting_job_settings

logger = logging.getLogger(__name__)

INGESTION_GROUP = "ingestion-workers"
INGESTION_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "ingestion_job_invalid"
FAILED_JOB_DETAIL = "ingestion_job_failed"
DEAD_LETTER_DETAIL = "ingestion_job_dead_lettered"
_FAULT_SOURCE = "ingestion"
_READ_BLOCK_MS = 1000


class IngestionHandler(Protocol):
    async def __call__(
        self,
        job: IngestionJob,
        *,
        access: OperatorAccess,
        ports: IngestPorts,
        ack: Callable[[], Awaitable[None]],
    ) -> IngestSummary: ...


def _missing_job_keys(error: BaseException) -> TypeGuard[FakeYouError]:
    return isinstance(error, FakeYouError) and error.code == ErrorCode.WORKER_JOB_KEYS_MISSING


def _ingestion_status_path(settings: Settings, meeting_id: str) -> Path:
    session_id = parse_session_id(meeting_id, field="meeting_id")
    return MeetingDir(settings.paths.record_dir, session_id).ingestion


@dataclass(frozen=True, slots=True)
class IngestionDeps:
    settings: Settings
    transport: StreamTransport
    store: SnapshotStore
    open_memory: Callable[[Settings], AbstractAsyncContextManager[Graphiti]]
    shutdown: asyncio.Event
    worker_id: str
    handler: IngestionHandler = handle_ingestion_job
    reconnect_pause_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class _Delivery:
    entry_id: str
    fields: dict[str, str]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _readable_meeting(fields: Mapping[str, str]) -> str | None:
    meeting_id = fields.get("meeting_id")
    if meeting_id is None or not is_identifier(meeting_id):
        return None
    return meeting_id


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


class IngestionConsumer:
    def __init__(self, deps: IngestionDeps) -> None:
        self._deps = deps
        self._transport = deps.transport
        lease_seconds = deps.settings.transport.redis.lease_seconds
        self._idle_ms = max(1, int(lease_seconds * 1000))
        self._heartbeat_seconds = lease_seconds / 3
        self._requeue_due = True

    async def run_forever(self) -> None:
        while True:
            try:
                if self._requeue_due:
                    await self.requeue_pending()
                if self._deps.shutdown.is_set():
                    return
                await self.run_once()
            except CONNECTION_ERRORS:
                self._requeue_due = True
                if await self._sleep_or_closed(self._deps.reconnect_pause_seconds):
                    return

    async def requeue_pending(self) -> list[str]:
        await self._transport.ensure_group(INGESTION_STREAM, INGESTION_GROUP)
        unacked = await self._transport.unacked_entries(INGESTION_STREAM, INGESTION_GROUP)
        queued = {fields.get("meeting_id") for _entry_id, fields in unacked}
        requeued = [
            meeting_id for meeting_id in self._pending_meetings() if meeting_id not in queued
        ]
        for meeting_id in requeued:
            await self._transport.publish_job(
                INGESTION_STREAM,
                encode_ingestion_job(meeting_id=meeting_id, kind="ingest", at=_now_iso()),
            )
        self._requeue_due = False
        return requeued

    async def run_once(self) -> IngestSummary | None:
        await self._transport.ensure_group(INGESTION_STREAM, INGESTION_GROUP)
        delivery = await self._next_delivery()
        if delivery is None:
            return None
        return await self._process(delivery)

    def _pending_meetings(self) -> list[str]:
        try:
            return pending_ingestion_meetings(self._deps.settings.paths.record_dir)
        except FakeYouError as error:
            logger.warning("pending ingestion scan failed: %s", error.code)
            return []

    async def _next_delivery(self) -> _Delivery | None:
        pending = await self._transport.pending_deliveries(
            INGESTION_STREAM, INGESTION_GROUP, min_idle_ms=self._idle_ms
        )
        for item in pending:
            if item.deliveries >= INGESTION_DELIVERY_LIMIT:
                await self._dead_letter(item)
                continue
            fields = await self._transport.claim_entry(
                INGESTION_STREAM,
                INGESTION_GROUP,
                self._deps.worker_id,
                item.entry_id,
                min_idle_ms=self._idle_ms,
            )
            if fields is not None:
                return _Delivery(item.entry_id, fields)
        fresh = await self._transport.read_group(
            INGESTION_STREAM,
            INGESTION_GROUP,
            self._deps.worker_id,
            count=1,
            block_ms=_READ_BLOCK_MS,
        )
        if not fresh:
            return None
        entry_id, fields = fresh[0]
        return _Delivery(entry_id, fields)

    async def _dead_letter(self, item: PendingDelivery) -> None:
        fields = await self._transport.read_entry(INGESTION_STREAM, item.entry_id) or {}
        await self._fault(fields, DEAD_LETTER_DETAIL, deliveries=str(item.deliveries))
        await self._transport.ack(INGESTION_STREAM, INGESTION_GROUP, item.entry_id)

    async def _process(self, delivery: _Delivery) -> IngestSummary | None:
        try:
            job = parse_ingestion_job(delivery.fields)
        except ValueError:
            await self._fault(delivery.fields, INVALID_JOB_DETAIL)
            return None
        reported: list[FakeYouError] = []
        async with asyncio.TaskGroup() as group:
            heartbeat = group.create_task(self._keep_claimed(delivery.entry_id))
            try:
                summary, error = await capture_error(
                    partial(self._handle, job, delivery.entry_id, reported)
                )
            finally:
                heartbeat.cancel()
        fields = delivery.fields
        for item in reported:
            await self._fault(fields, str(item.code))
        if error is not None:
            if _missing_job_keys(error):
                await self._record_missing_keys(job, delivery.entry_id, error)
            await self._fault(fields, FAILED_JOB_DETAIL, error_type=type(error).__name__)
            return None
        return summary

    async def _record_missing_keys(
        self, job: IngestionJob, entry_id: str, error: FakeYouError
    ) -> None:
        snapshot = self._deps.store.load(job.meeting_id)
        if snapshot is not None:
            write_status(
                _ingestion_status_path(self._deps.settings, job.meeting_id),
                IngestionStatus(
                    meeting_id=job.meeting_id,
                    group_id=memory_group_for(snapshot.owner_id, snapshot.twin_id),
                    status="failed",
                    reason=str(error.code),
                ),
            )
        await self._transport.ack(INGESTION_STREAM, INGESTION_GROUP, entry_id)

    async def _handle(
        self, job: IngestionJob, entry_id: str, reported: list[FakeYouError]
    ) -> IngestSummary:
        return await self._deps.handler(
            job,
            access=await self._access(job.meeting_id),
            ports=IngestPorts(
                stop_signals=_without_stop_signals, publish=_log_summary, report=reported.append
            ),
            ack=self._transport.create_entry_ack(INGESTION_STREAM, INGESTION_GROUP, entry_id),
        )

    async def _access(self, meeting_id: str) -> OperatorAccess:
        settings = await meeting_job_settings(
            self._deps.transport, self._deps.store, self._deps.settings, meeting_id
        )
        snapshot = self._deps.store.load(meeting_id)
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

    async def _keep_claimed(self, entry_id: str) -> None:
        while not await self._sleep_or_closed(self._heartbeat_seconds):
            try:
                await self._transport.touch_entry(
                    INGESTION_STREAM, INGESTION_GROUP, self._deps.worker_id, entry_id
                )
            except CONNECTION_ERRORS:
                return

    async def _fault(self, fields: Mapping[str, str], detail: str, **extra: str) -> None:
        meeting_id = _readable_meeting(fields)
        if meeting_id is None:
            logger.warning("ingestion entry without a readable meeting: %s", detail)
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

    async def _sleep_or_closed(self, seconds: float) -> bool:
        try:
            async with asyncio.timeout(seconds):
                await self._deps.shutdown.wait()
        except TimeoutError:
            return False
        return True
