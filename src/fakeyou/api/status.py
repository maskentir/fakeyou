from datetime import UTC, datetime
from typing import cast

from fastapi import Request

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.jobs import JobQueue
from fakeyou.db.session_status import SessionStatusRepository, SessionStatusStore


async def session_status(request: Request) -> SessionStatusRepository:
    store = cast("SessionStatusStore | None", request.app.state.session_status)
    if store is None:
        raise FakeYouError(ErrorCode.DATABASE_URL_MISSING)
    await store.require_current()
    return store


def status_now() -> datetime:
    return datetime.now(UTC)


async def queued_jobs(request: Request) -> JobQueue | None:
    return cast("JobQueue | None", request.app.state.jobs)


def require_jobs(queue: JobQueue | None) -> JobQueue:
    if queue is None:
        raise FakeYouError(ErrorCode.DATABASE_URL_MISSING)
    return queue
