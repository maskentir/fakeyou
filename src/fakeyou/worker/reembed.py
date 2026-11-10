import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Protocol, cast

from procrastinate import RetryStrategy
from procrastinate.job_context import JobContext
from procrastinate.worker import Worker

from fakeyou.config.identifiers import is_identifier
from fakeyou.config.settings import Settings
from fakeyou.db.jobs import REEMBED_QUEUE, REEMBED_TASK, JobQueue
from fakeyou.memory.maintenance import run_reembed
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.documents import write_session_persona
from fakeyou.worker.keys import job_settings

logger = logging.getLogger(__name__)

REEMBED_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "reembed_job_invalid"
FAILED_JOB_DETAIL = "reembed_job_failed"
DEAD_LETTER_DETAIL = "reembed_job_dead_lettered"


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
    async def __call__(self, access: OperatorAccess, *, job: ReembedJob) -> str: ...


@dataclass(frozen=True, slots=True)
class ReembedDeps:
    settings: Settings
    transport: StreamTransport
    store: TwinDirectory
    shutdown: asyncio.Event
    handler: ReembedHandler | None = None
    journal: Journal | None = None


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


async def run_reembed_job(access: OperatorAccess, *, job: ReembedJob) -> str:
    result = await run_reembed(access)
    logger.info("reembed job for %s finished: %s", job.twin_id, result)
    return job.twin_id


class ReembedWorker:
    def __init__(self, deps: ReembedDeps, queue: JobQueue) -> None:
        self._deps = deps
        self._handler = deps.handler or run_reembed_job
        self._queue = queue
        queue.app.task(
            name=REEMBED_TASK,
            queue=REEMBED_QUEUE,
            retry=RetryStrategy(max_attempts=REEMBED_DELIVERY_LIMIT, wait=5, exponential_wait=2),
        )(self._reembed_twin)

    async def run_forever(self) -> None:
        await self._queue.open()
        worker = Worker(app=self._queue.app, queues=[REEMBED_QUEUE], install_signal_handlers=False)
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
            queues=[REEMBED_QUEUE], wait=False, install_signal_handlers=False
        )

    async def _reembed_twin(
        self, owner_id: str, twin_id: str, context: JobContext | None = None
    ) -> str:
        if not is_identifier(owner_id) or not is_identifier(twin_id):
            self._fault(twin_id, INVALID_JOB_DETAIL)
            return twin_id
        job = ReembedJob(owner_id=owner_id, twin_id=twin_id)
        try:
            return await self._handler(await build_reembed_access(self._deps, job), job=job)
        except Exception as error:
            attempts = context.job.attempts if context is not None else 0
            detail = (
                DEAD_LETTER_DETAIL if attempts + 1 >= REEMBED_DELIVERY_LIMIT else FAILED_JOB_DETAIL
            )
            self._fault(twin_id, detail, error_type=type(error).__name__)
            raise

    def _fault(self, twin_id: str, detail: str, **extra: str) -> None:
        resolved = detail if is_identifier(twin_id) else f"{detail}:twin_unreadable"
        if self._deps.journal is not None:
            self._deps.journal.record(
                "reembed_failed", source="memory", detail=resolved, twin_id=twin_id, **extra
            )
        logger.warning("reembed job faulted: %s=%s %s", twin_id, resolved, extra)
