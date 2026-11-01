import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any

import pytest

from fakeyou.observability.cleanup import CLOSE_BUDGET_SECONDS, TeardownStack
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal


async def test_teardown_stack_budgets_each_callback() -> None:
    ran: list[str] = []

    async def hanging() -> None:
        await asyncio.sleep(30.0)

    async def fast() -> None:
        ran.append("fast")

    stack = TeardownStack(callback_budget_seconds=0.05)
    stack.push_async_callback(fast)
    stack.push_async_callback(hanging)
    started = time.monotonic()
    await stack.aclose()
    elapsed = time.monotonic() - started

    assert ran == ["fast"]
    assert elapsed < 5.0


async def test_teardown_stack_closes_every_layer_despite_cancel() -> None:
    ran: list[str] = []
    first_started = asyncio.Event()

    async def first() -> None:
        first_started.set()
        await asyncio.sleep(30.0)

    async def second() -> None:
        ran.append("second")

    async def third() -> None:
        ran.append("third")

    stack = TeardownStack(callback_budget_seconds=0.05)
    stack.push_async_callback(third)
    stack.push_async_callback(second)
    stack.push_async_callback(first)
    task = asyncio.create_task(stack.aclose())
    await first_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert ran == ["second", "third"]


async def test_teardown_stack_keeps_lifo_order_when_cancelled() -> None:
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()
        order.append("slow")

    async def fast() -> None:
        order.append("fast")

    stack = TeardownStack()
    stack.push_async_callback(fast)
    stack.push_async_callback(slow)
    task = asyncio.create_task(stack.aclose())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert order == ["slow", "fast"]


async def test_teardown_stack_records_dropped_callbacks_on_hard_deadline() -> None:
    ran: list[str] = []
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    async def slow() -> None:
        await asyncio.sleep(0.02)

    async def dropped() -> None:
        ran.append("dropped")

    stack = TeardownStack(callback_budget_seconds=0.05, faults=faults)
    stack.push_async_callback(dropped)
    stack.push_async_callback(dropped)
    stack.push_async_callback(slow)
    with pytest.raises(TimeoutError, match="hard deadline"):
        await stack.aclose(hard_timeout_seconds=0.01)

    assert ran == []
    (summary,) = faults.summaries()
    assert summary.operation == "teardown"
    assert summary.count == 2


async def test_teardown_stack_runs_context_exits_and_isolates_faults() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def boom() -> Any:
        events.append("enter")
        yield "value"
        events.append("exit")
        raise RuntimeError("boom")

    async def last() -> None:
        events.append("last")

    journal = RecordingJournal()
    stack = TeardownStack(callback_budget_seconds=5.0, faults=FaultReporter(journal=journal))
    value = await stack.enter_async_context(boom())
    stack.push_async_callback(last)
    errors = await stack.aclose()

    assert value == "value"
    assert events == ["enter", "last", "exit"]
    assert journal.events("fault_recorded") != []
    assert len(errors) == 1


async def test_teardown_stack_hard_deadline_stops_remaining_layers() -> None:
    attempted: list[str] = []

    async def hanging(name: str) -> None:
        attempted.append(name)
        await asyncio.sleep(30.0)

    stack = TeardownStack(callback_budget_seconds=0.05)
    for index in range(6):
        stack.push_async_callback(hanging, f"layer-{index}")
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="hard deadline"):
        await stack.aclose(hard_timeout_seconds=0.18)
    elapsed = time.monotonic() - started

    assert attempted
    assert len(attempted) < 6
    assert elapsed < 5.0


def test_close_budget_default_is_shared() -> None:
    import inspect

    from fakeyou.observability.cleanup import close_safely

    assert CLOSE_BUDGET_SECONDS == 8.0
    assert inspect.signature(close_safely).parameters["timeout_seconds"].default == 8.0
    assert (
        inspect.signature(TeardownStack.__init__).parameters["callback_budget_seconds"].default
        == 8.0
    )
