import asyncio
from collections.abc import Awaitable, Callable
from functools import partial
from typing import override

import pytest

from fakeyou.foundation.polling import PollGuard, PollingLoop


class ScriptedLoop(PollingLoop):
    def __init__(
        self,
        script: list[object],
        *,
        poll_interval_seconds: float = 0.01,
        monotonic: Callable[[], float] | None = None,
        guard: PollGuard | None = None,
    ) -> None:
        if monotonic is None:
            super().__init__(poll_interval_seconds=poll_interval_seconds, guard=guard)
        else:
            super().__init__(
                poll_interval_seconds=poll_interval_seconds, monotonic=monotonic, guard=guard
            )
        self._script = script
        self.runs = 0

    @override
    async def _poll_once(self) -> None:
        self.runs += 1
        action = self._script[min(self.runs - 1, len(self._script) - 1)]
        if isinstance(action, BaseException):
            raise action


def _live_poll_loops() -> int:
    count = 0
    for task in asyncio.all_tasks():
        if getattr(task.get_coro(), "__qualname__", "").endswith("_poll_loop"):
            count += 1
    return count


async def test_start_is_idempotent_and_close_stops_loop() -> None:
    loop = ScriptedLoop([None])
    await loop.start()
    await loop.start()
    assert loop.started
    assert _live_poll_loops() == 1
    await asyncio.sleep(0.03)
    assert loop.runs >= 1
    await loop.close()
    assert not loop.started
    runs = loop.runs
    await asyncio.sleep(0.03)
    assert loop.runs == runs


async def test_start_after_close_does_not_restart() -> None:
    loop = ScriptedLoop([None])
    await loop.close()
    await loop.start()
    assert not loop.started


async def test_poll_failure_is_recorded_and_loop_survives() -> None:
    from fakeyou.observability.faults import ConsumerSink, guard_poll

    guard = partial(guard_poll, sink=ConsumerSink(), source="runtime", operation="test")
    loop = ScriptedLoop([RuntimeError("gone"), None], poll_interval_seconds=0.01, guard=guard)
    await loop.start()
    await asyncio.sleep(0.05)
    await loop.close()
    assert loop.runs >= 2
    assert loop.poll_failures == 1
    assert loop.last_poll_error == "RuntimeError"


async def test_cancel_in_poll_ends_loop_without_failure_record() -> None:
    loop = ScriptedLoop([asyncio.CancelledError()])
    await loop.start()
    assert loop.started
    with pytest.raises(asyncio.CancelledError):
        await loop.wait()
    assert loop.poll_failures == 0
    await loop.close()


async def test_close_cancelled_by_caller_propagates_and_keeps_task() -> None:
    entered_cancel = asyncio.Event()
    release = asyncio.Event()

    class StubbornLoop(PollingLoop):
        @override
        async def _poll_once(self) -> None:
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                entered_cancel.set()
                await release.wait()
                raise

    loop = StubbornLoop(poll_interval_seconds=0.01)
    await loop.start()
    await asyncio.sleep(0)
    closer = asyncio.create_task(loop.close())
    await entered_cancel.wait()
    closer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closer
    assert loop.started
    release.set()
    await loop.close()
    assert not loop.started


async def test_uncompensated_loop_sleeps_full_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([10.0, 10.4])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    loop = ScriptedLoop([None], poll_interval_seconds=1.0, monotonic=lambda: next(ticks))
    await loop.start()
    assert loop.started
    with pytest.raises(asyncio.CancelledError):
        await loop.wait()
    assert delays == [1.0]


async def test_compensated_loop_subtracts_elapsed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks = iter([10.0, 10.4, 11.0, 12.5])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 2:
            raise asyncio.CancelledError

    class CompensatedLoop(ScriptedLoop):
        _COMPENSATE_SLEEP = True

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    loop = CompensatedLoop([None], poll_interval_seconds=1.0, monotonic=lambda: next(ticks))
    await loop.start()
    assert loop.started
    with pytest.raises(asyncio.CancelledError):
        await loop.wait()
    assert delays == [pytest.approx(0.6), 0.0]


async def test_unguarded_loop_dies_loud_on_poll_failure() -> None:
    loop = ScriptedLoop([KeyError("bug")], poll_interval_seconds=0.01)
    await loop.start()
    assert loop.started
    with pytest.raises(KeyError, match="bug"):
        await asyncio.wait_for(loop.wait(), timeout=2.0)
    with pytest.raises(KeyError, match="bug"):
        await loop.close()
    assert not loop.started


async def test_injected_guard_is_honored() -> None:
    calls: list[str] = []

    async def fake_guard(call: Callable[[], Awaitable[None]]) -> Exception | None:
        calls.append("guard")
        try:
            await call()
        except KeyError as error:
            return error
        return None

    loop = ScriptedLoop([KeyError("bug"), None], poll_interval_seconds=0.01, guard=fake_guard)
    await loop.start()
    await asyncio.sleep(0.05)
    await loop.close()
    assert calls != []
    assert loop.runs >= 2
    assert loop.poll_failures == 1
    assert loop.last_poll_error == "KeyError"
