import asyncio
import time

from fakeyou.foundation.clock import monotonic, using_clock
from fakeyou.observability.journal import Journal


class SteppedClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_the_default_clock_is_the_process_monotonic_clock() -> None:
    before = time.monotonic()
    reading = monotonic()
    after = time.monotonic()

    assert before <= reading <= after


def test_an_injected_clock_applies_only_inside_its_scope() -> None:
    clock = SteppedClock()
    with using_clock(clock):
        assert monotonic() == 100.0
        clock.now = 101.5
        assert monotonic() == 101.5

    assert monotonic() != clock.now


async def test_tasks_and_worker_threads_inherit_the_injected_clock() -> None:
    clock = SteppedClock()

    async def read() -> float:
        return monotonic()

    with using_clock(clock):
        in_task = await asyncio.create_task(read())
        in_thread = await asyncio.to_thread(monotonic)

    assert (in_task, in_thread) == (100.0, 100.0)


def test_journal_durations_follow_the_injected_clock() -> None:
    rows: list[str] = []
    journal = Journal("meeting-1", emit=rows.append, echo=False)
    clock = SteppedClock()

    with using_clock(clock):
        request_id = journal.request_started(source="llm", operation="chat")
        clock.now += 2.5
        journal.request_finished(request_id)

    assert '"duration_ms":2500.0' in rows[-1].replace(" ", "")
