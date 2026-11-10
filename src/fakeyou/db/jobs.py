from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from procrastinate import App, PsycopgConnector
from procrastinate.connector import BaseConnector

from fakeyou.config.settings import Settings
from fakeyou.db.engine import configured_url, require_url

REEMBED_QUEUE = "reembed"
REEMBED_TASK = "reembed_twin"
DEBRIEF_QUEUE = "debrief"
DEBRIEF_TASK = "debrief_meeting"
INGEST_QUEUE = "ingestion"
INGEST_TASK = "ingest_meeting"
INGEST_KINDS = ("ingest", "correct")


@dataclass(frozen=True, slots=True)
class QueuedJob:
    id: int
    task: str
    status: str
    args: dict[str, Any]


class JobQueue:
    def __init__(self, app: App) -> None:
        self._app = app
        self._opened = False

    @property
    def app(self) -> App:
        return self._app

    def task(self, **options: Any) -> Callable[..., Any]:
        name = options.get("name")
        if isinstance(name, str):
            self._app.tasks.pop(name, None)
        return cast(Callable[..., Any], self._app.task(**options))

    async def open(self) -> None:
        if not self._opened:
            await self._app.open_async()
            self._opened = True

    async def close(self) -> None:
        if self._opened:
            await self._app.close_async()
            self._opened = False

    async def defer_reembed(self, *, owner_id: str, twin_id: str) -> int:
        await self.open()
        task = self._app.configure_task(
            REEMBED_TASK, queue=REEMBED_QUEUE, lock=f"reembed:{twin_id}"
        )
        return await task.defer_async(owner_id=owner_id, twin_id=twin_id)

    async def defer_debrief(self, *, meeting_id: str, force: bool) -> int:
        await self.open()
        task = self._app.configure_task(
            DEBRIEF_TASK, queue=DEBRIEF_QUEUE, lock=f"debrief:{meeting_id}"
        )
        return await task.defer_async(meeting_id=meeting_id, force=force)

    async def defer_ingest(self, *, meeting_id: str, kind: str, delay_seconds: float = 0) -> int:
        if kind not in INGEST_KINDS:
            raise ValueError(f"unknown ingestion job kind: {kind!r}")
        await self.open()
        options: dict[str, Any] = {"queue": INGEST_QUEUE, "lock": f"ingest:{meeting_id}"}
        if delay_seconds > 0:
            options["schedule_in"] = {"seconds": delay_seconds}
        task = self._app.configure_task(INGEST_TASK, **options)
        return await task.defer_async(meeting_id=meeting_id, kind=kind)

    async def jobs_for(self, task: str) -> list[QueuedJob]:
        await self.open()
        jobs = await self._app.job_manager.list_jobs_async(task=task)
        return [
            QueuedJob(
                id=job.id,
                task=job.task_name,
                status=job.status or "todo",
                args=dict(job.task_kwargs or {}),
            )
            for job in jobs
            if job.id is not None
        ]

    async def fetch(self, job_id: int) -> QueuedJob | None:
        await self.open()
        jobs = await self._app.job_manager.list_jobs_async(id=job_id)
        for job in jobs:
            if job.id is None:
                continue
            return QueuedJob(
                id=job.id,
                task=job.task_name,
                status=job.status or "todo",
                args=dict(job.task_kwargs or {}),
            )
        return None


def build_job_queue(connector: BaseConnector) -> JobQueue:
    return JobQueue(App(connector=connector))


def create_job_queue(settings: Settings) -> JobQueue:
    url = require_url(configured_url(settings))
    return build_job_queue(PsycopgConnector(conninfo=url))
