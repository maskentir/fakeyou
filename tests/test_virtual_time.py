import asyncio
import time
import warnings
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from fakeyou.replay.virtual_time import VirtualTimeLoop


def run_virtual[T](main: Callable[[], Coroutine[Any, Any, T]]) -> T:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with asyncio.Runner(loop_factory=VirtualTimeLoop) as runner:
            return runner.run(main())


def test_an_hour_of_sleep_passes_in_virtual_time_only() -> None:
    async def main() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.sleep(3600)
        return loop.time() - start

    started = time.monotonic()
    elapsed = run_virtual(main)

    assert elapsed == pytest.approx(3600)
    assert time.monotonic() - started < 1


def test_timeouts_fire_at_their_virtual_deadline() -> None:
    async def main() -> float:
        loop = asyncio.get_running_loop()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=5)
        return loop.time()

    assert run_virtual(main) == pytest.approx(5)


def test_concurrent_sleepers_wake_in_virtual_order() -> None:
    async def main() -> list[tuple[str, float]]:
        loop = asyncio.get_running_loop()
        woke: list[tuple[str, float]] = []

        async def sleeper(name: str, seconds: float) -> None:
            await asyncio.sleep(seconds)
            woke.append((name, loop.time()))

        await asyncio.gather(sleeper("slow", 2.0), sleeper("fast", 0.5), sleeper("mid", 1.25))
        return woke

    assert run_virtual(main) == [("fast", 0.5), ("mid", 1.25), ("slow", 2.0)]


def test_work_in_a_thread_holds_virtual_time() -> None:
    async def main() -> tuple[float, float]:
        loop = asyncio.get_running_loop()
        finished: list[float] = []

        async def think() -> None:
            await asyncio.to_thread(time.sleep, 0.05)
            finished.append(loop.time())

        await asyncio.gather(think(), asyncio.sleep(10))
        return finished[0], loop.time()

    thread_done, end = run_virtual(main)

    assert thread_done == pytest.approx(0)
    assert end == pytest.approx(10)
