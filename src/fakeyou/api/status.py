from datetime import UTC, datetime
from typing import cast

from fastapi import Request

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.session_status import SessionStatusRepository, SessionStatusStore


async def session_status(request: Request) -> SessionStatusRepository:
    store = cast("SessionStatusStore | None", request.app.state.session_status)
    if store is None:
        raise FakeYouError(ErrorCode.DATABASE_URL_MISSING)
    await store.require_current()
    return store


def status_now() -> datetime:
    return datetime.now(UTC)
