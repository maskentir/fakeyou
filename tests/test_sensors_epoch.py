import asyncio
from collections.abc import Callable
from typing import Any, override

import pytest

from fakeyou.foundation.clock_mapping import ClockExchange
from fakeyou.perception.sensors import ActiveSampler, DrainBatch, PerceptionDrain
from fakeyou.perception.timeline import SCREEN_SOURCE_ID
from tests.test_sensors import JOINED_MS, FakePerceptionPage, message, people, sensors
from tests.test_sensors_samples import ROADMAP, sample, sequenced


def page_batch(page_session: str, generation: int, sequence: int, at_ms: float) -> DrainBatch:
    return DrainBatch.model_validate(
        {
            "page_session": page_session,
            "drained_page_ms": at_ms,
            "messages": [{**message(people()).model_dump(), "page_session": page_session}],
            "samples": [
                {
                    **sample(
                        "stream-screen",
                        "screen",
                        sequence,
                        generation=generation,
                        page_ms=at_ms,
                        media_timestamp=float(sequence),
                    ).model_dump(),
                    "page_session": page_session,
                }
            ],
        }
    )


async def test_new_page_epoch_accepts_reset_sampler_generation_and_counters() -> None:
    sensed, timeline, _ = sensors()
    await sensed.accept_batch(page_batch("page-a", 8, 500, JOINED_MS + 1000))
    previous = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert previous is not None
    sensed.begin_epoch()

    await sensed.accept_batch(page_batch("page-b", 1, 1, JOINED_MS + 2000))

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.record.generation == 1
    assert latest.record.sequence == 1
    assert latest.record.media_timestamp == 1.0
    assert previous.record.generation == 8


async def test_retired_page_cannot_replace_new_page_with_a_higher_generation() -> None:
    sensed, timeline, _ = sensors()
    await sensed.accept_batch(page_batch("page-a", 1, 1, JOINED_MS + 1000))
    sensed.begin_epoch()
    await sensed.accept_batch(page_batch("page-b", 2, 2, JOINED_MS + 2000))
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None

    await sensed.accept_batch(page_batch("page-a", 99, 999, JOINED_MS + 3000))

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == latest


@pytest.mark.parametrize("kind", ["messages", "samples", "frames", "active"])
async def test_new_batch_cannot_relabel_retired_page_parts(kind: str) -> None:
    sensed, timeline, journal = sensors()
    old = page_batch("page-a", 8, 500, JOINED_MS + 1000)
    await sensed.accept_batch(old)
    sensed.begin_epoch()
    await sensed.accept_batch(page_batch("page-b", 1, 1, JOINED_MS + 2000))
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    old_frame = sequenced("stream-screen", "screen", ROADMAP, 500).model_copy(
        update={"page_session": "page-a", "generation": 8}
    )
    parts = {
        "messages": old.messages,
        "samples": old.samples,
        "frames": [old_frame],
        "active": [
            ActiveSampler(
                page_session="page-a", stream_id="stream-budi", role="camera", generation=8
            )
        ],
    }
    mixed = page_batch("page-b", 99, 999, JOINED_MS + 3000).model_copy(update={kind: parts[kind]})

    assert await sensed.accept_batch(mixed) is False

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == latest
    assert journal.events("perception_batch_rejected")[-1]["reason"] == "page_session_mismatch"


async def test_generation_regression_is_still_rejected_within_the_same_page() -> None:
    sensed, timeline, _ = sensors()
    await sensed.accept_batch(page_batch("page-a", 8, 500, JOINED_MS + 1000))
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)

    await sensed.accept_batch(page_batch("page-a", 1, 501, JOINED_MS + 2000))

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == latest


class HeldPage(FakePerceptionPage):
    def __init__(self, batch: DrainBatch) -> None:
        super().__init__([batch.model_dump()])
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def evaluate(self, expression: str, arg: Any = None, /) -> Any:
        self.started.set()
        await self.release.wait()
        return await super().evaluate(expression, arg)


async def test_drain_in_flight_cannot_cross_a_python_page_epoch() -> None:
    sensed, timeline, _ = sensors()
    page = HeldPage(page_batch("page-a", 8, 500, JOINED_MS + 1000))
    drain = PerceptionDrain(page, sensed)
    task = asyncio.create_task(drain.drain_once())
    await page.started.wait()
    sensed.begin_epoch()
    page.release.set()
    await task

    assert drain.batches_rejected == 1
    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) is None
    assert sensed.page_clock.map(JOINED_MS + 1000) is None
    assert page.assigned == []


async def test_reload_detected_by_the_drain_starts_a_new_page_and_reassigns_streams() -> None:
    sensed, timeline, _ = sensors()
    page = FakePerceptionPage(
        [
            page_batch("page-a", 8, 500, JOINED_MS + 1000).model_dump(),
            page_batch("page-b", 1, 1, JOINED_MS + 2000).model_dump(),
            page_batch("page-a", 99, 999, JOINED_MS + 3000).model_dump(),
        ]
    )
    drain = PerceptionDrain(page, sensed)
    await drain.drain_once()
    original = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert original is not None
    await drain.drain_once()
    current = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert current is not None
    assert sensed.epoch == 1
    assert current.record.page_epoch == 1
    assert current.record.page_session == "page-b"
    assert current.record.generation == 1
    assert current.continuity > original.continuity
    assert len(page.assigned) == 2
    await drain.drain_once()

    assert drain.batches_rejected == 1
    assert len(page.assigned) == 2
    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == current
    assert original.record.page_session == "page-a"
    assert original.record.generation == 8


async def test_new_epoch_does_not_reuse_the_previous_page_clock_exchange() -> None:
    sensed, timeline, _ = sensors()
    sensed.monotonic = lambda: 100.0
    sensed.page_clock.observe(ClockExchange(JOINED_MS + 1000, 99.98, 100.0))
    await sensed.accept_batch(page_batch("page-a", 8, 500, JOINED_MS + 1000))
    original = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert original is not None
    assert original.capture is not None
    original_capture = original.capture

    sensed.begin_epoch()
    assert sensed.page_clock.map(JOINED_MS + 1000) is None
    await sensed.accept_batch(page_batch("page-b", 1, 1, JOINED_MS + 2000))
    current = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert current is not None
    assert current.capture is None
    assert original.capture == original_capture


async def test_missing_page_session_cannot_bind_a_new_production_epoch() -> None:
    sensed, timeline, journal = sensors()
    sensed.begin_epoch()

    assert await sensed.accept_batch(DrainBatch(), expected_epoch=1) is False

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) is None
    assert journal.events("perception_batch_rejected")[-1]["reason"] == "page_session_missing"


async def test_decode_in_flight_does_not_admit_the_rest_of_a_retired_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensed, timeline, _ = sensors()
    started, release = asyncio.Event(), asyncio.Event()

    async def held_decode(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return function(*args, **kwargs)

    monkeypatch.setattr("fakeyou.perception.sensors.asyncio.to_thread", held_decode)
    old_frame = sequenced("stream-screen", "screen", ROADMAP, 500).model_copy(
        update={"page_session": "page-a", "generation": 8}
    )
    old_batch = page_batch("page-a", 8, 500, JOINED_MS + 1000).model_copy(
        update={"frames": [old_frame, old_frame]}
    )
    task = asyncio.create_task(sensed.accept_batch(old_batch))
    await asyncio.wait_for(started.wait(), 2.0)
    sensed.begin_epoch()
    await sensed.accept_batch(page_batch("page-b", 1, 1, JOINED_MS + 2000))
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    release.set()

    assert await task is False

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == latest
    assert timeline.snapshot().current_screen is None
    assert sensed.frames_seen == 0
