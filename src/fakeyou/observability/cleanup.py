import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import Any, Self, TypeVar

from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import classify_exception

CLOSE_BUDGET_SECONDS = 8.0

_T = TypeVar("_T")


async def _attempt_close(
    close: Callable[[], Awaitable[None]],
    *,
    source: Subsystem,
    operation: str,
    faults: FaultReporter | None,
    timeout_seconds: float,
) -> Exception | None:
    try:
        async with asyncio.timeout(timeout_seconds):
            await close()
    except Exception as error:
        if faults is not None:
            faults.record(classify_exception(source, error, operation=operation))
        return error
    return None


async def close_safely(
    close: Callable[[], Awaitable[None]],
    *,
    source: Subsystem,
    operation: str = "close",
    faults: FaultReporter | None = None,
    timeout_seconds: float = CLOSE_BUDGET_SECONDS,
) -> None:
    await _attempt_close(
        close,
        source=source,
        operation=operation,
        faults=faults,
        timeout_seconds=timeout_seconds,
    )


class TeardownStack:
    def __init__(
        self,
        *,
        callback_budget_seconds: float = CLOSE_BUDGET_SECONDS,
        source: Subsystem = "runtime",
        faults: FaultReporter | None = None,
    ) -> None:
        self._budget = callback_budget_seconds
        self._source = source
        self._faults = faults
        self._pending: list[Callable[[], Awaitable[None]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def enter_async_context(self, cm: AbstractAsyncContextManager[_T]) -> _T:
        value = await cm.__aenter__()

        async def exit_context() -> None:
            await cm.__aexit__(None, None, None)

        self._pending.append(exit_context)
        return value

    def push_async_callback(
        self, callback: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Callable[..., Any]:
        async def invoke() -> None:
            result = callback(*args, **kwargs)
            if inspect.isawaitable(result):
                await result

        self._pending.append(invoke)
        return callback

    async def _close_one(self, close: Callable[[], Awaitable[None]]) -> Exception | None:
        return await _attempt_close(
            close,
            source=self._source,
            operation="teardown",
            faults=self._faults,
            timeout_seconds=self._budget,
        )

    def _expire_pending(self) -> TimeoutError:
        expired = TimeoutError("teardown hard deadline exceeded")
        while self._pending:
            self._pending.pop()
            if self._faults is not None:
                self._faults.record(classify_exception(self._source, expired, operation="teardown"))
        return expired

    async def aclose(self, *, hard_timeout_seconds: float | None = None) -> list[Exception]:
        started = time.monotonic()
        cancelled: asyncio.CancelledError | None = None
        errors: list[Exception] = []
        while self._pending:
            if (
                hard_timeout_seconds is not None
                and time.monotonic() - started > hard_timeout_seconds
            ):
                raise self._expire_pending()
            close = self._pending.pop()
            closing = asyncio.create_task(self._close_one(close))
            try:
                error = await asyncio.shield(closing)
            except asyncio.CancelledError as cancel:
                if cancelled is None:
                    cancelled = cancel
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        continue
                if closing.cancelled():
                    continue
                error = closing.result()
            if error is not None:
                errors.append(error)
        if cancelled is not None:
            raise cancelled
        return errors
