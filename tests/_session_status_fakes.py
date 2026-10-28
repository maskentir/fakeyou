from dataclasses import replace
from datetime import datetime

from fakeyou.db.session_status import DesiredState, SessionStatus
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason


class MemorySessionStatus:
    def __init__(self) -> None:
        self.rows: dict[str, SessionStatus] = {}

    def create(self, meeting_id: str, owner_id: str, at: datetime) -> SessionStatus | None:
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

    def get(self, meeting_id: str) -> SessionStatus | None:
        return self.rows.get(meeting_id)

    def list_for(self, owner_id: str) -> list[SessionStatus]:
        owned = [status for status in self.rows.values() if status.owner_id == owner_id]
        return sorted(owned, key=lambda status: (status.created_at, status.meeting_id))

    def desire(self, meeting_id: str, state: DesiredState, at: datetime) -> SessionStatus | None:
        current = self.rows.get(meeting_id)
        if current is None or current.desired_state == state or current.phase in TERMINAL_PHASES:
            return None
        updated = replace(current, desired_state=state, updated_at=at)
        self.rows[meeting_id] = updated
        return updated

    def record_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        at: datetime,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> SessionStatus | None:
        current = self.rows.get(meeting_id)
        if current is None or current.phase in TERMINAL_PHASES:
            return None
        updated = replace(
            current, phase=phase, terminal_reason=terminal_reason, detail=detail, updated_at=at
        )
        self.rows[meeting_id] = updated
        return updated

    def discard(self, meeting_id: str) -> None:
        self.rows.pop(meeting_id, None)
