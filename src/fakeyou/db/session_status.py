from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DisconnectionError, OperationalError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import configured_url, create_engine_for_url, require_url
from fakeyou.db.schema import require_current
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

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES


class SessionStatusRepository(Protocol):
    def create(self, meeting_id: str, owner_id: str, at: datetime) -> SessionStatus | None: ...

    def get(self, meeting_id: str) -> SessionStatus | None: ...

    def list_for(self, owner_id: str) -> list[SessionStatus]: ...

    def desire(
        self, meeting_id: str, state: DesiredState, at: datetime
    ) -> SessionStatus | None: ...

    def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None: ...

    def discard(self, meeting_id: str) -> None: ...


@contextmanager
def _reachable() -> Iterator[None]:
    try:
        yield
    except (OperationalError, DisconnectionError) as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error


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
    )


class SessionStatusStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def close(self) -> None:
        self._engine.dispose()

    def require_current(self) -> None:
        with _reachable():
            require_current(self._engine)

    def create(self, meeting_id: str, owner_id: str, at: datetime) -> SessionStatus | None:
        statement = (
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
        with _reachable(), self._engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else _status(row)

    def get(self, meeting_id: str) -> SessionStatus | None:
        statement = select(meeting_sessions).where(meeting_sessions.c.meeting_id == meeting_id)
        with _reachable(), self._engine.connect() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else _status(row)

    def list_for(self, owner_id: str) -> list[SessionStatus]:
        statement = (
            select(meeting_sessions)
            .where(meeting_sessions.c.owner_id == owner_id)
            .order_by(meeting_sessions.c.created_at, meeting_sessions.c.meeting_id)
        )
        with _reachable(), self._engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [_status(row) for row in rows]

    def desire(self, meeting_id: str, state: DesiredState, at: datetime) -> SessionStatus | None:
        statement = (
            update(meeting_sessions)
            .where(
                meeting_sessions.c.meeting_id == meeting_id,
                meeting_sessions.c.desired_state != state,
                meeting_sessions.c.phase.not_in(sorted(TERMINAL_PHASES)),
            )
            .values(desired_state=state, updated_at=at)
            .returning(meeting_sessions)
        )
        with _reachable(), self._engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else _status(row)

    def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None:
        statement = (
            update(meeting_sessions)
            .where(
                meeting_sessions.c.meeting_id == meeting_id,
                meeting_sessions.c.phase.not_in(sorted(TERMINAL_PHASES)),
            )
            .values(phase=phase, terminal_reason=terminal_reason, detail=detail, updated_at=at)
            .returning(meeting_sessions)
        )
        with _reachable(), self._engine.begin() as connection:
            row = connection.execute(statement).mappings().first()
        return None if row is None else _status(row)

    def discard(self, meeting_id: str) -> None:
        statement = delete(meeting_sessions).where(meeting_sessions.c.meeting_id == meeting_id)
        with _reachable(), self._engine.begin() as connection:
            connection.execute(statement)


def create_session_status(settings: Settings) -> SessionStatusStore:
    database = settings.database
    return SessionStatusStore(
        create_engine_for_url(
            require_url(configured_url(settings)),
            connect_timeout_seconds=database.connect_timeout_seconds,
            statement_timeout_seconds=database.statement_timeout_seconds,
        )
    )
