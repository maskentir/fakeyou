import asyncio
import selectors
from collections.abc import Callable
from concurrent.futures import Executor
from typing import Any, override


class VirtualSelector(selectors.DefaultSelector):
    def __init__(self) -> None:
        super().__init__()
        self.elapsed = 0.0
        self.busy = 0

    @override
    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if self.busy or timeout is None or timeout <= 0:
            return super().select(timeout)
        ready = super().select(0)
        if not ready:
            self.elapsed += timeout
        return ready


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    def __init__(self, start: float = 0.0) -> None:
        self._virtual = VirtualSelector()
        self._start = start
        super().__init__(self._virtual)

    @override
    def time(self) -> float:
        return self._start + self._virtual.elapsed

    @override
    def run_in_executor[T, *Ts](
        self, executor: Executor | None, func: Callable[[*Ts], T], *args: *Ts
    ) -> asyncio.Future[T]:
        self._virtual.busy += 1
        future = super().run_in_executor(executor, func, *args)
        future.add_done_callback(self._settle)
        return future

    @override
    async def shutdown_default_executor(self, timeout: float | None = None) -> None:
        self._virtual.busy += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self._virtual.busy -= 1

    def _settle(self, _future: asyncio.Future[Any]) -> None:
        self._virtual.busy -= 1
