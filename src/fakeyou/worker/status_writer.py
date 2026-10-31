import asyncio
import logging
from datetime import UTC, datetime
from functools import partial

from tenacity import AsyncRetrying, RetryCallState, stop_after_attempt
from tenacity.wait import wait_chain, wait_fixed

from fakeyou.db.session_status import PhaseUpdate, SessionStatusRepository
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason

logger = logging.getLogger(__name__)

TERMINAL_RETRY_SECONDS = (1.0, 2.0, 4.0, 8.0, 16.0)


class StatusWriter:
    def __init__(
        self,
        status: SessionStatusRepository,
        meeting_id: str,
        *,
        attempt_id: str | None = None,
        retry_seconds: tuple[float, ...] = TERMINAL_RETRY_SECONDS,
    ) -> None:
        self._status = status
        self._meeting_id = meeting_id
        self._attempt_id = attempt_id
        self._retry_seconds = retry_seconds
        self._queue: asyncio.Queue[PhaseUpdate] = asyncio.Queue()
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
        self._queue.put_nowait(
            PhaseUpdate(phase, datetime.now(UTC), terminal_reason=terminal_reason, detail=detail)
        )
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

    async def _persist(self, write: PhaseUpdate) -> None:
        attempts = len(self._retry_seconds) + 1 if write.phase in TERMINAL_PHASES else 1
        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_chain(*map(wait_fixed, self._retry_seconds or (0.0,))),
            after=partial(self._log_failure, write.phase),
            retry_error_callback=lambda _state: None,
        )
        async for attempt in retrying:
            with attempt:
                await self._status.record_phase(
                    self._meeting_id, write, attempt_id=self._attempt_id
                )

    def _log_failure(self, phase: PublicPhase, state: RetryCallState) -> None:
        error = state.outcome.exception() if state.outcome is not None else None
        logger.warning(
            "status write %s for meeting %s failed: %s",
            phase,
            self._meeting_id,
            type(error).__name__,
        )
