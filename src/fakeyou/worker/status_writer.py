import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fakeyou.catalog.errors import FakeYouError
from fakeyou.db.session_status import SessionStatusRepository
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PhaseWrite:
    phase: PublicPhase
    terminal_reason: TerminalReason | None
    detail: str | None
    at: datetime


class StatusWriter:
    def __init__(self, status: SessionStatusRepository, meeting_id: str) -> None:
        self._status = status
        self._meeting_id = meeting_id
        self._queue: asyncio.Queue[PhaseWrite] = asyncio.Queue()
        self._task = asyncio.create_task(self._drain(), name=f"status-writer-{meeting_id}")
        self.phase: PublicPhase | None = None

    def note(
        self,
        phase: PublicPhase,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> bool:
        if phase == self.phase and terminal_reason is None and detail is None:
            return False
        self.phase = phase
        self._queue.put_nowait(PhaseWrite(phase, terminal_reason, detail, datetime.now(UTC)))
        return True

    async def write(
        self,
        phase: PublicPhase,
        *,
        terminal_reason: TerminalReason | None = None,
        detail: str | None = None,
    ) -> bool:
        changed = self.note(phase, terminal_reason=terminal_reason, detail=detail)
        await self._queue.join()
        return changed

    async def close(self) -> None:
        await self._queue.join()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _drain(self) -> None:
        while True:
            write = await self._queue.get()
            try:
                await asyncio.to_thread(
                    self._status.record_phase,
                    self._meeting_id,
                    write.phase,
                    write.at,
                    terminal_reason=write.terminal_reason,
                    detail=write.detail,
                )
            except (FakeYouError, OSError):
                logger.exception("status write for meeting %s failed", self._meeting_id)
            finally:
                self._queue.task_done()
