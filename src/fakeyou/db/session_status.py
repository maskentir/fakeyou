from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, cast

from sqlalchemy import ColumnElement, Executable, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import configured_url, create_async_engine_for_url, require_url
from fakeyou.db.schema import require_current_on
from fakeyou.db.tables import meeting_sessions
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason

DesiredState = Literal["created", "joined", "stopped"]


@dataclass(frozen=True, slots=True)
class SessionStatus:
    meeting_id: str
    owner_id: str
    desired_state: DesiredState
    phase: PublicPhase
    terminal_reason: TerminalReason | None
    detail: str | None
    attempt_id: str | None
    created_at: datetime
    updated_at: datetime
    attempts: int = 0
    lease_owner: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


@dataclass(frozen=True, slots=True)
class PhaseUpdate:
    phase: PublicPhase
    at: datetime
    terminal_reason: TerminalReason | None = None
    detail: str | None = None


class SessionStatusRepository(Protocol):
    async def create(
        self, meeting_id: str, owner_id: str, at: datetime
    ) -> SessionStatus | None: ...

    async def get(self, meeting_id: str) -> SessionStatus | None: ...

    async def list_for(self, owner_id: str) -> list[SessionStatus]: ...

    async def desire(
        self, meeting_id: str, state: DesiredState, at: datetime
    ) -> SessionStatus | None: ...

    async def record_phase(
        self, meeting_id: str, change: PhaseUpdate, *, attempt_id: str | None = None
    ) -> SessionStatus | None: ...

    async def discard(self, meeting_id: str) -> None: ...

    async def claim(
        self, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None: ...

    async def renew(
        self, meeting_id: str, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None: ...

    async def release(self, meeting_id: str, worker_id: str, attempt_id: str) -> None: ...


def _status(row: RowMapping) -> SessionStatus:
    return SessionStatus(
        meeting_id=row["meeting_id"],
        owner_id=row["owner_id"],
        desired_state=cast(DesiredState, row["desired_state"]),
        phase=cast(PublicPhase, row["phase"]),
        terminal_reason=cast(TerminalReason | None, row["terminal_reason"]),
        detail=row["detail"],
        attempt_id=row["attempt_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        attempts=row["attempts"],
        lease_owner=row["lease_owner"],
    )


@contextmanager
def _reachable() -> Iterator[None]:
    try:
        yield
    except (OperationalError, DisconnectionError) as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error


class SessionStatusStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._schema_current = False

    async def close(self) -> None:
        await self._engine.dispose()

    async def require_current(self) -> None:
        if self._schema_current:
            return
        with _reachable():
            async with self._engine.connect() as connection:
                await connection.run_sync(require_current_on)
        self._schema_current = True

    async def create(self, meeting_id: str, owner_id: str, at: datetime) -> SessionStatus | None:
        return await self._first(
            insert(meeting_sessions)
            .values(
                meeting_id=meeting_id,
                owner_id=owner_id,
                desired_state="created",
                phase="created",
                created_at=at,
                updated_at=at,
            )
            .on_conflict_do_nothing(index_elements=[meeting_sessions.c.meeting_id])
            .returning(meeting_sessions)
        )

    async def get(self, meeting_id: str) -> SessionStatus | None:
        return await self._first(
            select(meeting_sessions).where(meeting_sessions.c.meeting_id == meeting_id)
        )

    async def list_for(self, owner_id: str) -> list[SessionStatus]:
        statement = (
            select(meeting_sessions)
            .where(meeting_sessions.c.owner_id == owner_id)
            .order_by(meeting_sessions.c.created_at, meeting_sessions.c.meeting_id)
        )
        with _reachable():
            async with self._engine.connect() as connection:
                rows = (await connection.execute(statement)).mappings().all()
        return [_status(row) for row in rows]

    async def desire(
        self, meeting_id: str, state: DesiredState, at: datetime
    ) -> SessionStatus | None:
        return await self._first(
            update(meeting_sessions)
            .where(
                meeting_sessions.c.meeting_id == meeting_id,
                meeting_sessions.c.desired_state != state,
                meeting_sessions.c.phase.not_in(sorted(TERMINAL_PHASES)),
            )
            .values(desired_state=state, updated_at=at)
            .returning(meeting_sessions)
        )

    async def record_phase(
        self, meeting_id: str, change: PhaseUpdate, *, attempt_id: str | None = None
    ) -> SessionStatus | None:
        fenced = [] if attempt_id is None else [meeting_sessions.c.attempt_id == attempt_id]
        return await self._first(
            update(meeting_sessions)
            .where(
                meeting_sessions.c.meeting_id == meeting_id,
                meeting_sessions.c.phase.not_in(sorted(TERMINAL_PHASES)),
                *fenced,
            )
            .values(
                phase=change.phase,
                terminal_reason=change.terminal_reason,
                detail=change.detail,
                updated_at=change.at,
                **({"attempts": 0} if change.phase == "active" else {}),
            )
            .returning(meeting_sessions)
        )

    async def claim(
        self, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None:
        free = (
            select(meeting_sessions.c.meeting_id)
            .where(
                meeting_sessions.c.desired_state.in_(("joined", "stopped")),
                meeting_sessions.c.phase.not_in(sorted(TERMINAL_PHASES)),
                or_(
                    meeting_sessions.c.lease_expires_at.is_(None),
                    meeting_sessions.c.lease_expires_at < func.now(),
                ),
            )
            .order_by(meeting_sessions.c.updated_at, meeting_sessions.c.meeting_id)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        return await self._first(
            update(meeting_sessions)
            .where(meeting_sessions.c.meeting_id == free)
            .values(
                lease_owner=worker_id,
                lease_expires_at=func.now() + timedelta(seconds=lease_seconds),
                attempt_id=attempt_id,
                attempts=meeting_sessions.c.attempts + 1,
            )
            .returning(meeting_sessions)
        )

    async def renew(
        self, meeting_id: str, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None:
        return await self._first(
            update(meeting_sessions)
            .where(*self._held(meeting_id, worker_id, attempt_id))
            .values(lease_expires_at=func.now() + timedelta(seconds=lease_seconds))
            .returning(meeting_sessions)
        )

    async def release(self, meeting_id: str, worker_id: str, attempt_id: str) -> None:
        await self._first(
            update(meeting_sessions)
            .where(*self._held(meeting_id, worker_id, attempt_id))
            .values(lease_owner=None, lease_expires_at=None)
            .returning(meeting_sessions)
        )

    @staticmethod
    def _held(meeting_id: str, worker_id: str, attempt_id: str) -> tuple[ColumnElement[bool], ...]:
        return (
            meeting_sessions.c.meeting_id == meeting_id,
            meeting_sessions.c.lease_owner == worker_id,
            meeting_sessions.c.attempt_id == attempt_id,
        )

    async def discard(self, meeting_id: str) -> None:
        with _reachable():
            async with self._engine.begin() as connection:
                await connection.execute(
                    delete(meeting_sessions).where(meeting_sessions.c.meeting_id == meeting_id)
                )

    async def _first(self, statement: Executable) -> SessionStatus | None:
        with _reachable():
            async with self._engine.begin() as connection:
                row = (await connection.execute(statement)).mappings().first()
        return None if row is None else _status(row)


def create_session_status(settings: Settings) -> SessionStatusStore:
    database = settings.database
    url = require_url(configured_url(settings))
    return SessionStatusStore(
        create_async_engine_for_url(
            url,
            connect_timeout_seconds=database.connect_timeout_seconds,
            statement_timeout_seconds=database.statement_timeout_seconds,
        )
    )
