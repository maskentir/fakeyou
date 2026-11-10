import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast

from procrastinate import RetryStrategy
from procrastinate.job_context import JobContext
from procrastinate.worker import Worker

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import is_identifier, parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.jobs import DEBRIEF_QUEUE, DEBRIEF_TASK, JobQueue
from fakeyou.debrief.debrief import DebriefModels
from fakeyou.debrief.debrief_service import (
    DebriefOutcome,
    DebriefRequest,
    DebriefWiring,
    run_debrief,
)
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.providers import create_debrief_chat_model, create_fallback_chat_model
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.consumer import SnapshotStore
from fakeyou.worker.keys import meeting_job_settings

logger = logging.getLogger(__name__)

DEBRIEF_DELIVERY_LIMIT = 5
INVALID_JOB_DETAIL = "debrief_job_invalid"
FAILED_JOB_DETAIL = "debrief_job_failed"
DEAD_LETTER_DETAIL = "debrief_job_dead_lettered"
_FAULT_SOURCE = "debrief"


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
    ) -> DebriefOutcome: ...


@dataclass(frozen=True, slots=True)
class DebriefDeps:
    settings: Settings
    transport: StreamTransport
    store: SnapshotStore
    shutdown: asyncio.Event
    handler: DebriefHandler | None = None
    journal: Journal | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _error_fields(error: Exception) -> dict[str, str]:
    fields = {"error_type": type(error).__name__}
    code = getattr(error, "code", None)
    if code is not None:
        fields["error_code"] = str(code.value if hasattr(code, "value") else code)
    return fields


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


async def run_debrief_job(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
    request = await build_debrief_request(deps, job)
    return await run_debrief(
        request,
        DebriefWiring(
            models=partial(debrief_models, request.settings),
            stop_signals=stop_signal_handlers,
        ),
    )


class DebriefWorker:
    def __init__(self, deps: DebriefDeps, queue: JobQueue) -> None:
        self._deps = deps
        self._handler = deps.handler or run_debrief_job
        self._transport = deps.transport
        self._queue = queue
        queue.task(
            pass_context=True,
            name=DEBRIEF_TASK,
            queue=DEBRIEF_QUEUE,
            retry=RetryStrategy(
                max_attempts=DEBRIEF_DELIVERY_LIMIT - 1, wait=5, exponential_wait=2
            ),
        )(self._debrief_meeting)

    async def run_forever(self) -> None:
        await self._queue.open()
        worker = Worker(app=self._queue.app, queues=[DEBRIEF_QUEUE], install_signal_handlers=False)
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
            queues=[DEBRIEF_QUEUE], wait=False, install_signal_handlers=False
        )

    async def _debrief_meeting(
        self, context: JobContext, meeting_id: str, *, force: bool
    ) -> DebriefOutcome | None:
        if not is_identifier(meeting_id):
            await self._fault(meeting_id, INVALID_JOB_DETAIL)
            return None
        job = DebriefJob(meeting_id=meeting_id, force=force)
        try:
            outcome = await self._handler(job, deps=self._deps)
        except Exception as error:
            attempts = context.job.attempts
            detail = (
                DEAD_LETTER_DETAIL if attempts + 1 >= DEBRIEF_DELIVERY_LIMIT else FAILED_JOB_DETAIL
            )
            await self._fault(meeting_id, detail, **_error_fields(error))
            raise
        logger.info("debrief job for %s finished", job.meeting_id)
        return outcome

    async def _fault(self, meeting_id: str, detail: str, **extra: str) -> None:
        if not is_identifier(meeting_id):
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
