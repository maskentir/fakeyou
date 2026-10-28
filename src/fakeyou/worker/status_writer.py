import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial

from fakeyou.db.session_status import SessionStatusRepository
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason
from fakeyou.observability.faults import capture_error

logger = logging.getLogger(__name__)

TERMINAL_RETRY_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0)


@dataclass(frozen=True, slots=True)
class PhaseWrite:
    phase: PublicPhase
    terminal_reason: TerminalReason | None
    detail: str | None
    at: datetime


class StatusWriter:
    def __init__(
        self,
        status: SessionStatusRepository,
        meeting_id: str,
        *,
        retry_seconds: tuple[float, ...] = TERMINAL_RETRY_SECONDS,
    ) -> None:
        self._status = status
        self._meeting_id = meeting_id
        self._retry_seconds = retry_seconds
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
                await self._persist(write)
            finally:
                self._queue.task_done()

    async def _persist(self, write: PhaseWrite) -> None:
        retries = self._retry_seconds if write.phase in TERMINAL_PHASES else ()
        for delay in (*retries, None):
            _, error = await capture_error(partial(self._record, write))
            if error is None:
                return
            logger.warning(
                "status write %s for meeting %s failed: %s",
                write.phase,
                self._meeting_id,
                type(error).__name__,
            )
            if delay is None:
                return
            await asyncio.sleep(delay)

    async def _record(self, write: PhaseWrite) -> None:
        await asyncio.to_thread(
            self._status.record_phase,
            self._meeting_id,
            write.phase,
            write.at,
            terminal_reason=write.terminal_reason,
            detail=write.detail,
        )
