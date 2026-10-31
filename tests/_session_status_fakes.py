from dataclasses import replace
from datetime import datetime

from fakeyou.db.session_status import DesiredState, PhaseUpdate, SessionStatus
from fakeyou.foundation.taxonomy import TERMINAL_PHASES


class MemorySessionStatus:
    def __init__(self) -> None:
        self.rows: dict[str, SessionStatus] = {}
        self.leases: dict[str, str] = {}
        self.expired: set[str] = set()
        self.renewals = 0

    async def create(self, meeting_id: str, owner_id: str, at: datetime) -> SessionStatus | None:
        if meeting_id in self.rows:
            return None
        status = SessionStatus(
            meeting_id=meeting_id,
            owner_id=owner_id,
            desired_state="created",
            phase="created",
            terminal_reason=None,
            detail=None,
            attempt_id=None,
            created_at=at,
            updated_at=at,
        )
        self.rows[meeting_id] = status
        return status

    async def get(self, meeting_id: str) -> SessionStatus | None:
        return self.rows.get(meeting_id)

    async def list_for(self, owner_id: str) -> list[SessionStatus]:
        owned = [status for status in self.rows.values() if status.owner_id == owner_id]
        return sorted(owned, key=lambda status: (status.created_at, status.meeting_id))

    async def desire(
        self, meeting_id: str, state: DesiredState, at: datetime
    ) -> SessionStatus | None:
        current = self.rows.get(meeting_id)
        if current is None or current.desired_state == state or current.phase in TERMINAL_PHASES:
            return None
        updated = replace(current, desired_state=state, updated_at=at)
        self.rows[meeting_id] = updated
        return updated

    async def record_phase(
        self, meeting_id: str, change: PhaseUpdate, *, attempt_id: str | None = None
    ) -> SessionStatus | None:
        current = self.rows.get(meeting_id)
        if current is None or current.phase in TERMINAL_PHASES:
            return None
        if attempt_id is not None and current.attempt_id != attempt_id:
            return None
        updated = replace(
            current,
            phase=change.phase,
            terminal_reason=change.terminal_reason,
            detail=change.detail,
            updated_at=change.at,
            attempts=0 if change.phase == "active" else current.attempts,
        )
        self.rows[meeting_id] = updated
        return updated

    async def discard(self, meeting_id: str) -> None:
        self.rows.pop(meeting_id, None)

    async def claim(
        self, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None:
        del lease_seconds
        for meeting_id, status in sorted(self.rows.items(), key=lambda item: item[1].updated_at):
            free = self.leases.get(meeting_id) is None or meeting_id in self.expired
            if status.desired_state in ("joined", "stopped") and not status.terminal and free:
                self.leases[meeting_id] = worker_id
                self.expired.discard(meeting_id)
                claimed = replace(
                    status,
                    attempt_id=attempt_id,
                    attempts=status.attempts + 1,
                    lease_owner=worker_id,
                )
                self.rows[meeting_id] = claimed
                return claimed
        return None

    async def renew(
        self, meeting_id: str, worker_id: str, attempt_id: str, lease_seconds: float
    ) -> SessionStatus | None:
        del lease_seconds
        status = self.rows.get(meeting_id)
        if status is None or self.leases.get(meeting_id) != worker_id:
            return None
        if status.attempt_id != attempt_id:
            return None
        self.renewals += 1
        return status

    async def release(self, meeting_id: str, worker_id: str, attempt_id: str) -> None:
        status = self.rows.get(meeting_id)
        if status is not None and status.attempt_id == attempt_id:
            if self.leases.get(meeting_id) == worker_id:
                self.leases.pop(meeting_id)
