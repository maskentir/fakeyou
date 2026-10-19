import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from fakeyou.config.identifiers import is_identifier
from fakeyou.config.settings import Settings
from fakeyou.memory.maintenance import run_reembed
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.transport.schemas import REEMBED_STREAM
from fakeyou.transport.streams import CONNECTION_ERRORS, PendingDelivery, StreamTransport
from fakeyou.worker.documents import write_session_persona
from fakeyou.worker.keys import job_settings

logger = logging.getLogger(__name__)

REEMBED_GROUP = "reembed-workers"
REEMBED_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "reembed_job_invalid"
FAILED_JOB_DETAIL = "reembed_job_failed"
DEAD_LETTER_DETAIL = "reembed_job_dead_lettered"
CONCURRENT_DETAIL = "reembed_job_already_running"
_READ_BLOCK_MS = 1000


@dataclass(frozen=True, slots=True)
class ReembedJob:
    owner_id: str
    twin_id: str


class TwinSnapshot(Protocol):
    @property
    def meeting_id(self) -> str: ...

    @property
    def twin_id(self) -> str: ...

    @property
    def persona(self) -> PersonaProfile | None: ...


class TwinDirectory(Protocol):
    def load(self, meeting_id: str) -> object | None: ...

    def list_for(self, owner_id: str) -> list[TwinSnapshot]: ...


class ReembedHandler(Protocol):
    async def __call__(
        self, access: OperatorAccess, *, job: ReembedJob, ack: Callable[[], Awaitable[None]]
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ReembedDeps:
    settings: Settings
    transport: StreamTransport
    store: TwinDirectory
    shutdown: asyncio.Event
    worker_id: str
    handler: ReembedHandler | None = None
    journal: Journal | None = None
    reconnect_pause_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class _Delivery:
    entry_id: str
    fields: dict[str, str]


def _identity(fields: Mapping[str, str], name: str) -> str:
    value = fields.get(name, "")
    if not is_identifier(value):
        raise ValueError(f"unreadable {name}: {value!r}")
    return value


def parse_reembed_job(fields: Mapping[str, str]) -> ReembedJob:
    if fields.get("type") != "reembed_job":
        raise ValueError(f"unknown reembed job type: {fields.get('type')!r}")
    return ReembedJob(owner_id=_identity(fields, "owner_id"), twin_id=_identity(fields, "twin_id"))


async def build_reembed_access(deps: ReembedDeps, job: ReembedJob) -> OperatorAccess:
    settings = await job_settings(
        deps.transport, deps.settings, f"twin:{job.twin_id}", required=False
    )
    persona_path = None
    for snapshot in deps.store.list_for(job.owner_id):
        if snapshot.twin_id == job.twin_id and snapshot.persona is not None:
            persona_path = write_session_persona(
                settings.paths.record_dir, snapshot.meeting_id, snapshot.persona
            )
            break
    return OperatorAccess(
        OperatorIdentity(persona_path, job.owner_id, job.twin_id),
        settings,
        partial(open_persona_memory, settings),
    )


async def run_reembed_job(
    access: OperatorAccess, *, job: ReembedJob, ack: Callable[[], Awaitable[None]]
) -> str:
    result = await run_reembed(access)
    await ack()
    logger.info("reembed job for %s finished: %s", job.twin_id, result)
    return job.twin_id


class ReembedConsumer:
    def __init__(self, deps: ReembedDeps) -> None:
        self._deps = deps
        self._handler = deps.handler or run_reembed_job
        self._transport = deps.transport
        lease_seconds = deps.settings.transport.redis.lease_seconds
        self._idle_ms = max(1, int(lease_seconds * 1000))
        self._heartbeat_seconds = lease_seconds / 3
        self._running: set[str] = set()

    async def run_forever(self) -> None:
        while True:
            try:
                if self._deps.shutdown.is_set():
                    return
                await self.run_once()
            except CONNECTION_ERRORS:
                if await self._sleep_or_closed(self._deps.reconnect_pause_seconds):
                    return

    async def run_once(self) -> str | None:
        await self._transport.ensure_group(REEMBED_STREAM, REEMBED_GROUP)
        delivery = await self._next_delivery()
        if delivery is None:
            return None
        return await self._process(delivery)

    async def _next_delivery(self) -> _Delivery | None:
        pending = await self._transport.pending_deliveries(
            REEMBED_STREAM, REEMBED_GROUP, min_idle_ms=self._idle_ms
        )
        for item in pending:
            if item.deliveries >= REEMBED_DELIVERY_LIMIT:
                await self._dead_letter(item)
                continue
            fields = await self._transport.claim_entry(
                REEMBED_STREAM,
                REEMBED_GROUP,
                self._deps.worker_id,
                item.entry_id,
                min_idle_ms=self._idle_ms,
            )
            if fields is not None:
                return _Delivery(item.entry_id, fields)
        fresh = await self._transport.read_group(
            REEMBED_STREAM,
            REEMBED_GROUP,
            self._deps.worker_id,
            count=1,
            block_ms=_READ_BLOCK_MS,
        )
        if not fresh:
            return None
        entry_id, fields = fresh[0]
        return _Delivery(entry_id, dict(fields))

    async def _dead_letter(self, item: PendingDelivery) -> None:
        fields = await self._transport.read_entry(REEMBED_STREAM, item.entry_id) or {}
        self._fault(fields, DEAD_LETTER_DETAIL, deliveries=str(item.deliveries))
        await self._transport.ack(REEMBED_STREAM, REEMBED_GROUP, item.entry_id)

    async def _process(self, delivery: _Delivery) -> str | None:
        try:
            job = parse_reembed_job(delivery.fields)
        except ValueError:
            self._fault(delivery.fields, INVALID_JOB_DETAIL)
            return None
        if job.twin_id in self._running:
            self._fault(delivery.fields, CONCURRENT_DETAIL)
            return None
        self._running.add(job.twin_id)
        try:
            async with asyncio.TaskGroup() as group:
                heartbeat = group.create_task(self._keep_claimed(delivery.entry_id))
                try:
                    twin_id, error = await capture_error(
                        partial(self._handle, job, delivery.entry_id)
                    )
                finally:
                    heartbeat.cancel()
        finally:
            self._running.discard(job.twin_id)
        if error is not None:
            self._fault(delivery.fields, FAILED_JOB_DETAIL, error_type=type(error).__name__)
            return None
        return twin_id

    async def _handle(self, job: ReembedJob, entry_id: str) -> str:
        return await self._handler(
            await build_reembed_access(self._deps, job),
            job=job,
            ack=self._transport.create_entry_ack(REEMBED_STREAM, REEMBED_GROUP, entry_id),
        )

    async def _keep_claimed(self, entry_id: str) -> None:
        while not await self._sleep_or_closed(self._heartbeat_seconds):
            try:
                await self._transport.touch_entry(
                    REEMBED_STREAM, REEMBED_GROUP, self._deps.worker_id, entry_id
                )
            except CONNECTION_ERRORS:
                return

    def _fault(self, fields: Mapping[str, str], detail: str, **extra: str) -> None:
        twin_id = fields.get("twin_id", "")
        resolved = detail if is_identifier(twin_id) else f"{detail}:twin_unreadable"
        if self._deps.journal is not None:
            self._deps.journal.record(
                "reembed_failed", source="memory", detail=resolved, twin_id=twin_id, **extra
            )
        logger.warning("reembed job faulted: %s=%s %s", twin_id, resolved, extra)

    async def _sleep_or_closed(self, seconds: float) -> bool:
        try:
            async with asyncio.timeout(seconds):
                await self._deps.shutdown.wait()
        except TimeoutError:
            return False
        return True
