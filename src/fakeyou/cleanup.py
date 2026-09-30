import asyncio
from collections.abc import Awaitable, Callable

from fakeyou.faults import FaultReporter
from fakeyou.provider_faults import classify_exception


async def close_safely(
    close: Callable[[], Awaitable[None]],
    *,
    source: str,
    faults: FaultReporter | None = None,
    timeout_seconds: float = 8.0,
) -> None:
    try:
        async with asyncio.timeout(timeout_seconds):
            await close()
    except Exception as error:
        if faults is not None:
            faults.record(classify_exception(source, error))
