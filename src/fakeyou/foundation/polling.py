import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class PageEvaluator(Protocol):
    async def evaluate(self, expression: str, /) -> Any: ...


PollGuard = Callable[[Callable[[], Awaitable[None]]], Awaitable[Exception | None]]


@dataclass(frozen=True, slots=True)
class PollSchedule:
    interval_seconds: float
    monotonic: Callable[[], float] = time.monotonic


async def _die_loud(call: Callable[[], Awaitable[None]]) -> Exception | None:
    await call()
    return None


class PollingLoop:
    _COMPENSATE_SLEEP = False

    def __init__(
        self,
        *,
        poll_interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        guard: PollGuard | None = None,
    ) -> None:
        self._poll_interval = poll_interval_seconds
        self._monotonic = monotonic
        self._guard = guard if guard is not None else _die_loud
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self.polls = 0
        self.poll_failures = 0
        self.last_poll_error: str | None = None

    @property
    def started(self) -> bool:
        return self._task is not None

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def start(self) -> None:
        if self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        self._closed = True
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is not None and caller.cancelling() > 0:
                    self._task = task
                    raise

    async def _poll_loop(self) -> None:
        while True:
            started = self._monotonic()
            error = await self._guard(self._poll_once)
            if error is not None:
                self._record_poll_failure(error)
            elapsed = self._monotonic() - started
            await asyncio.sleep(self._sleep_delay(elapsed))

    async def _poll_once(self) -> None:
        raise NotImplementedError

    def _record_poll_failure(self, error: Exception) -> None:
        self.poll_failures += 1
        self.last_poll_error = type(error).__name__

    def _sleep_delay(self, elapsed: float) -> float:
        if not self._COMPENSATE_SLEEP:
            return self._poll_interval
        return max(0.0, self._poll_interval - elapsed)
