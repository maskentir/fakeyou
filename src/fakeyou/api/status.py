import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import Request

from fakeyou.config.settings import Settings
from fakeyou.db.session_status import SessionStatus, SessionStatusRepository, create_session_status

if TYPE_CHECKING:
    from fakeyou.api.app import ApiDeps


def session_status(request: Request) -> SessionStatusRepository:
    state = request.app.state
    factory = cast("ApiDeps", state.deps).session_status
    if factory is not None:
        return cast(SessionStatusRepository, factory())
    if state.session_status is None:
        store = create_session_status(cast(Settings, state.settings))
        store.require_current()
        state.session_status = store
    return cast(SessionStatusRepository, state.session_status)


async def read_status(request: Request, meeting_id: str) -> SessionStatus | None:
    return await asyncio.to_thread(session_status(request).get, meeting_id)


async def status_call[T](request: Request, call: Callable[[SessionStatusRepository], T]) -> T:
    return await asyncio.to_thread(call, session_status(request))


def status_now() -> datetime:
    return datetime.now(UTC)
