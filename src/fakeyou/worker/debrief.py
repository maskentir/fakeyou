import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Protocol

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import is_identifier, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import DebriefModels
from fakeyou.debrief.debrief_service import (
    DebriefOutcome,
    DebriefRequest,
    DebriefWiring,
    run_debrief,
)
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.providers import create_debrief_chat_model, create_fallback_chat_model
from fakeyou.transport.schemas import DEBRIEF_STREAM
from fakeyou.transport.streams import CONNECTION_ERRORS, PendingDelivery, StreamTransport
from fakeyou.worker.consumer import SnapshotStore
from fakeyou.worker.keys import meeting_job_settings

logger = logging.getLogger(__name__)

DEBRIEF_GROUP = "debrief-workers"
DEBRIEF_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "debrief_job_invalid"
FAILED_JOB_DETAIL = "debrief_job_failed"
DEAD_LETTER_DETAIL = "debrief_job_dead_lettered"
_FAULT_SOURCE = "debrief"
_READ_BLOCK_MS = 1000


@dataclass(frozen=True, slots=True)
class DebriefJob:
    meeting_id: str
    force: bool


class DebriefHandler(Protocol):
    async def __call__(
        self,
        job: DebriefJob,
        *,
        deps: "DebriefDeps",
        ack: Callable[[], Awaitable[None]],
    ) -> DebriefOutcome: ...


@dataclass(frozen=True, slots=True)
class DebriefDeps:
    settings: Settings
    transport: StreamTransport
    store: SnapshotStore
    shutdown: asyncio.Event
    worker_id: str
    handler: DebriefHandler | None = None
    journal: Journal | None = None
    reconnect_pause_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class _Delivery:
    entry_id: str
    fields: dict[str, str]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _error_fields(error: Exception) -> dict[str, str]:
    fields = {"error_type": type(error).__name__}
    code = getattr(error, "code", None)
    if code is not None:
        fields["error_code"] = str(code.value if hasattr(code, "value") else code)
    return fields


def parse_debrief_job(fields: Mapping[str, str]) -> DebriefJob:
    if fields.get("type") != "debrief_job":
        raise ValueError(f"unknown debrief job type: {fields.get('type')!r}")
    meeting_id = fields.get("meeting_id", "")
    if not is_identifier(meeting_id):
        raise ValueError(f"unreadable meeting id: {meeting_id!r}")
    force = fields.get("force", "false")
    if force not in ("true", "false"):
        raise ValueError(f"unreadable force flag: {force!r}")
    return DebriefJob(meeting_id=meeting_id, force=force == "true")


def debrief_models(settings: Settings) -> DebriefModels:
    return DebriefModels(create_debrief_chat_model(settings), create_fallback_chat_model(settings))


def _operator(store: SnapshotStore, meeting_id: str) -> PersonaProfile:
    snapshot = store.load(meeting_id)
    if snapshot is None or snapshot.persona is None:
        raise FakeYouError(ErrorCode.MEMORY_PERSONA_UNREADABLE, reason=meeting_id)
    return snapshot.persona


async def build_debrief_request(deps: DebriefDeps, job: DebriefJob) -> DebriefRequest:
    settings = await meeting_job_settings(deps.transport, deps.store, deps.settings, job.meeting_id)
    return DebriefRequest(
        meeting_id=parse_session_id(job.meeting_id, field="meeting_id"),
        operator=_operator(deps.store, job.meeting_id),
        settings=settings,
        force=job.force,
    )


async def run_debrief_job(
    job: DebriefJob, *, deps: DebriefDeps, ack: Callable[[], Awaitable[None]]
) -> DebriefOutcome:
    request = await build_debrief_request(deps, job)
    outcome = await run_debrief(
        request,
        DebriefWiring(
            models=partial(debrief_models, request.settings),
            stop_signals=stop_signal_handlers,
        ),
    )
    await ack()
    return outcome


class DebriefConsumer:
    def __init__(self, deps: DebriefDeps) -> None:
        self._deps = deps
        self._handler = deps.handler or run_debrief_job
        self._transport = deps.transport
        lease_seconds = deps.settings.transport.redis.lease_seconds
        self._idle_ms = max(1, int(lease_seconds * 1000))
        self._heartbeat_seconds = lease_seconds / 3

    async def run_forever(self) -> None:
        while True:
            try:
                if self._deps.shutdown.is_set():
                    return
                await self.run_once()
            except CONNECTION_ERRORS:
                if await self._sleep_or_closed(self._deps.reconnect_pause_seconds):
                    return

    async def run_once(self) -> DebriefOutcome | None:
        await self._transport.ensure_group(DEBRIEF_STREAM, DEBRIEF_GROUP)
        delivery = await self._next_delivery()
        if delivery is None:
            return None
        return await self._process(delivery)

    async def _next_delivery(self) -> _Delivery | None:
        pending = await self._transport.pending_deliveries(
            DEBRIEF_STREAM, DEBRIEF_GROUP, min_idle_ms=self._idle_ms
        )
        for item in pending:
            if item.deliveries >= DEBRIEF_DELIVERY_LIMIT:
                await self._dead_letter(item)
                continue
            fields = await self._transport.claim_entry(
                DEBRIEF_STREAM,
                DEBRIEF_GROUP,
                self._deps.worker_id,
                item.entry_id,
                min_idle_ms=self._idle_ms,
            )
            if fields is not None:
                return _Delivery(item.entry_id, fields)
        fresh = await self._transport.read_group(
            DEBRIEF_STREAM,
            DEBRIEF_GROUP,
            self._deps.worker_id,
            count=1,
            block_ms=_READ_BLOCK_MS,
        )
        if not fresh:
            return None
        entry_id, fields = fresh[0]
        return _Delivery(entry_id, dict(fields))

    async def _dead_letter(self, item: PendingDelivery) -> None:
        fields = await self._transport.read_entry(DEBRIEF_STREAM, item.entry_id) or {}
        await self._fault(fields, DEAD_LETTER_DETAIL, deliveries=str(item.deliveries))
        await self._transport.ack(DEBRIEF_STREAM, DEBRIEF_GROUP, item.entry_id)

    async def _process(self, delivery: _Delivery) -> DebriefOutcome | None:
        try:
            job = parse_debrief_job(delivery.fields)
        except ValueError:
            await self._fault(delivery.fields, INVALID_JOB_DETAIL)
            return None
        async with asyncio.TaskGroup() as group:
            heartbeat = group.create_task(self._keep_claimed(delivery.entry_id))
            try:
                outcome, error = await capture_error(partial(self._handle, job, delivery.entry_id))
            finally:
                heartbeat.cancel()
        if error is not None:
            await self._fault(delivery.fields, FAILED_JOB_DETAIL, **_error_fields(error))
            return None
        logger.info("debrief job for %s finished", job.meeting_id)
        return outcome

    async def _handle(self, job: DebriefJob, entry_id: str) -> DebriefOutcome:
        return await self._handler(
            job,
            deps=self._deps,
            ack=self._transport.create_entry_ack(DEBRIEF_STREAM, DEBRIEF_GROUP, entry_id),
        )

    async def _keep_claimed(self, entry_id: str) -> None:
        while not await self._sleep_or_closed(self._heartbeat_seconds):
            try:
                await self._transport.touch_entry(
                    DEBRIEF_STREAM, DEBRIEF_GROUP, self._deps.worker_id, entry_id
                )
            except CONNECTION_ERRORS:
                return

    async def _fault(self, fields: Mapping[str, str], detail: str, **extra: str) -> None:
        meeting_id = fields.get("meeting_id")
        if meeting_id is None or not is_identifier(meeting_id):
            logger.warning("debrief entry without a readable meeting: %s", detail)
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
