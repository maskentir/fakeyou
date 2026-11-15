from typing import Any

import pytest

from fakeyou.foundation.clock_mapping import ClockExchange
from fakeyou.foundation.content_digest import (
    DECODED_PIXELS_SHA256,
    HEARTBEAT_WITHOUT_RECORD,
    ContentSample,
)
from fakeyou.foundation.publication import SnapshotRef
from fakeyou.perception.sensors import (
    ActiveSampler,
    DrainBatch,
    DrainedFrame,
    DrainedSample,
    PerceptionDrain,
)
from fakeyou.perception.timeline import SCREEN_SOURCE_ID, MeetingTimeline
from tests._meet_protocol_fakes import collection_payload, output
from tests._perception_fakes import slide_jpeg
from tests.test_camera_evidence import jpeg_of
from tests.test_sensors import (
    BUDI,
    JOINED_MS,
    FakePerceptionPage,
    frame,
    message,
    people,
    sensors,
)

ROADMAP = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])


def sample(
    stream_id: str, role: str, sequence: int, digest: str | None = "aa", **changes: Any
) -> DrainedSample:
    return DrainedSample.model_validate(
        {
            "stream_id": stream_id,
            "role": role,
            "page_ms": JOINED_MS + 1000 * sequence,
            "sequence": sequence,
            "media_timestamp": 33_000.0 * sequence,
            "width": 1920,
            "height": 1080,
            "pixel_format": "I420",
            "algorithm": DECODED_PIXELS_SHA256,
            "version": 1,
            "digest": digest,
            **changes,
        }
    )


def sequenced(stream_id: str, role: str, data: bytes, sequence: int) -> DrainedFrame:
    return frame(stream_id, role, data, at_ms=JOINED_MS + 1000 * sequence).model_copy(
        update={"sequence": sequence}
    )


async def test_screen_sample_reaches_the_ledger_with_its_page_record() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(messages=[message(people())], samples=[sample("stream-screen", "screen", 1)])
    )

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.record.algorithm == DECODED_PIXELS_SHA256
    assert (latest.record.width, latest.record.height, latest.record.pixel_format) == (
        1920,
        1080,
        "I420",
    )
    assert latest.record.stream_id == "stream-screen"
    assert latest.record.media_timestamp == 33_000.0
    assert sensed.samples_seen == 1


async def test_capture_time_is_the_page_time_of_the_frame_not_of_the_drain() -> None:
    sensed, timeline, _ = sensors()
    sensed.monotonic = lambda: 500.0
    assert sensed.page_clock.observe(ClockExchange(JOINED_MS + 9000, 499.98, 500.0))

    await sensed.accept_batch(
        DrainBatch(messages=[message(people())], samples=[sample("stream-screen", "screen", 1)])
    )

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.capture is not None
    assert latest.capture.monotonic == pytest.approx(491.99, abs=0.001)
    assert latest.capture.uncertainty_seconds < 0.05


async def test_sample_without_a_clock_exchange_is_stored_unmapped() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(messages=[message(people())], samples=[sample("stream-screen", "screen", 1)])
    )

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.capture is None


async def test_camera_sample_is_keyed_by_the_participant() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(messages=[message(people())], samples=[sample("stream-budi", "camera", 1)])
    )

    latest = timeline.samples.latest("camera", BUDI)
    assert latest is not None
    assert latest.record.subject_id == BUDI


async def test_sample_of_an_unassigned_or_mismatched_stream_is_ignored() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[sample("stream-unknown", "screen", 1), sample("stream-budi", "screen", 1)],
        )
    )

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) is None
    assert timeline.samples.latest("camera", BUDI) is None
    assert sensed.samples_seen == 0


async def test_emitted_frame_is_bound_to_the_page_record_of_its_sequence() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[sample("stream-screen", "screen", 4, "frame-digest")],
            frames=[sequenced("stream-screen", "screen", ROADMAP, 4)],
        )
    )

    analyzed = timeline.analyzed_sample(current_screen_ref(timeline))
    assert analyzed is not None
    assert analyzed.record.digest == "frame-digest"
    assert analyzed.record.sequence == 4
    assert analyzed.record.algorithm == DECODED_PIXELS_SHA256


def current_screen_ref(timeline: MeetingTimeline) -> SnapshotRef:
    segment = timeline.snapshot().current_screen
    assert segment is not None
    keyframe = timeline.latest_keyframe(segment)
    return SnapshotRef("screen", segment.ref, keyframe.sha256, 0, 0, 0, keyframe.captured_at)


async def test_frame_whose_record_never_arrived_falls_back_to_a_byte_record() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[sequenced("stream-screen", "screen", ROADMAP, 4)],
        )
    )
    await sensed.accept_batch(DrainBatch(samples=[sample("stream-screen", "screen", 5)]))

    analyzed = timeline.analyzed_sample(current_screen_ref(timeline))
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert analyzed is not None
    assert latest is not None
    assert analyzed.record.same_content_as(latest.record) is None


async def test_skipped_hash_is_carried_with_its_reason() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[
                sample("stream-screen", "screen", 1),
                sample("stream-screen", "screen", 2, None, skip_reason="hash_over_budget"),
            ],
        )
    )

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.record.digest is None
    assert latest.record.skip_reason == "hash_over_budget"


async def test_out_of_order_and_repeated_samples_are_journaled_and_not_latest() -> None:
    sensed, timeline, journal = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[
                sample("stream-screen", "screen", 3),
                sample("stream-screen", "screen", 2, "bb"),
                sample("stream-screen", "screen", 4, "cc", media_timestamp=99_000.0),
            ],
        )
    )

    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.record.sequence == 3
    assert [row["outcome"] for row in journal.events("perception_sample_rejected")] == [
        "out_of_order",
        "repeated",
    ]
    assert sensed.samples_irregular == 2


async def camera_with_heartbeat(*, records: bool) -> ContentSample | None:
    sensed, timeline, _ = sensors()
    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[frame("stream-budi", "camera", ROADMAP)],
            samples=[sample("stream-budi", "camera", 1)] if records else [],
        )
    )
    await sensed.accept_batch(
        DrainBatch(
            active=[
                ActiveSampler(
                    stream_id="stream-budi", role="camera", last_sample_page_ms=JOINED_MS + 9000
                )
            ]
        )
    )
    return timeline.samples.latest("camera", BUDI)


async def test_heartbeat_of_a_stream_without_records_is_an_unverified_sample() -> None:
    latest = await camera_with_heartbeat(records=False)

    assert latest is not None
    assert latest.record.skip_reason == HEARTBEAT_WITHOUT_RECORD
    assert latest.record.digest is None


async def test_heartbeat_of_a_stream_that_sends_records_adds_no_sample() -> None:
    latest = await camera_with_heartbeat(records=True)

    assert latest is not None
    assert latest.record.skip_reason is None


async def test_sampler_restart_resets_counters_without_binding_a_retired_frame() -> None:
    sensed, timeline, journal = sensors()
    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[sample("stream-screen", "screen", 500, generation=1)],
        )
    )
    await sensed.accept_batch(
        DrainBatch(
            samples=[
                sample(
                    "stream-screen",
                    "screen",
                    1,
                    "new-frame",
                    generation=2,
                    page_ms=JOINED_MS + 501_000,
                    media_timestamp=1.0,
                )
            ],
            frames=[
                sequenced("stream-screen", "screen", ROADMAP, 1).model_copy(
                    update={
                        "generation": 2,
                        "page_ms": JOINED_MS + 501_000,
                    }
                )
            ],
        )
    )
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert latest.record.generation == 2
    assert timeline.analyzed_sample(current_screen_ref(timeline)) == latest
    before = current_screen_ref(timeline)

    await sensed.accept_batch(
        DrainBatch(
            samples=[
                sample(
                    "stream-screen",
                    "screen",
                    501,
                    "retired",
                    generation=1,
                    page_ms=JOINED_MS + 502_000,
                )
            ],
            frames=[
                sequenced("stream-screen", "screen", ROADMAP, 501).model_copy(
                    update={
                        "generation": 1,
                        "page_ms": JOINED_MS + 502_000,
                    }
                )
            ],
        )
    )

    assert timeline.samples.latest("screen", SCREEN_SOURCE_ID) == latest
    assert current_screen_ref(timeline) == before
    assert journal.events("perception_sample_rejected")[-1]["outcome"] == "retired_stream"
    assert journal.events("perception_frame_dropped")[-1]["reason"] == "retired_stream"


def generated(
    stream_id: str, role: str, data: bytes, sequence: int, generation: int
) -> DrainedFrame:
    return sequenced(stream_id, role, data, sequence).model_copy(update={"generation": generation})


async def test_frame_drained_before_its_sample_is_shown_once_the_sample_arrives() -> None:
    sensed, timeline, journal = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[generated("stream-screen", "screen", ROADMAP, 1, 1)],
        )
    )
    waiting = timeline.snapshot().current_screen
    await sensed.accept_batch(
        DrainBatch(samples=[sample("stream-screen", "screen", 1, generation=1)])
    )

    assert waiting is None
    shown = timeline.snapshot().current_screen
    assert shown is not None
    latest = timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert (latest.record.generation, latest.record.sequence) == (1, 1)
    assert journal.events("perception_frame_dropped") == []


async def test_frame_whose_sample_never_arrives_is_dropped_after_one_more_drain() -> None:
    sensed, timeline, journal = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[generated("stream-screen", "screen", ROADMAP, 1, 1)],
        )
    )
    await sensed.accept_batch(DrainBatch())
    await sensed.accept_batch(
        DrainBatch(samples=[sample("stream-screen", "screen", 1, generation=1)])
    )

    assert timeline.snapshot().current_screen is None
    assert [item["reason"] for item in journal.events("perception_frame_dropped")] == [
        "sample_missing"
    ]


async def test_camera_turned_off_and_on_inside_one_drain_restarts_its_sampler() -> None:
    sensed, timeline, _ = sensors()
    grey = jpeg_of((120, 120, 120))
    off = message(
        collection_payload(outputs=[output(BUDI, "stream-budi", disabled=True)]),
        at_ms=JOINED_MS + 1500,
    )
    on = message(collection_payload(outputs=[output(BUDI, "stream-budi")]), at_ms=JOINED_MS + 1600)
    batches = [
        DrainBatch(
            messages=[message(people())],
            samples=[sample("stream-budi", "camera", 1, generation=3)],
            frames=[generated("stream-budi", "camera", grey, 1, 3)],
        ),
        DrainBatch(
            messages=[off, on],
            samples=[sample("stream-budi", "camera", 2, "bb", generation=3)],
            frames=[generated("stream-budi", "camera", grey, 2, 3)],
        ),
        DrainBatch(
            samples=[
                sample("stream-budi", "camera", 1, "cc", generation=4, page_ms=JOINED_MS + 3000)
            ],
            frames=[
                generated("stream-budi", "camera", grey, 1, 4).model_copy(
                    update={"page_ms": JOINED_MS + 3000}
                )
            ],
        ),
    ]
    page = FakePerceptionPage([item.model_dump() for item in batches])
    drain = PerceptionDrain(page, sensed)

    await drain.drain_once()
    await drain.drain_once()
    retired = timeline.camera_frames.get(BUDI)
    await drain.drain_once()

    assert retired is None
    streams = [[item["stream_id"] for item in pushed] for pushed in page.assigned]
    assert "stream-budi" in streams[0]
    assert "stream-budi" not in streams[1]
    assert "stream-budi" in streams[2]
    restarted = timeline.camera_frames.get(BUDI)
    assert restarted is not None
    assert not timeline.samples.stopped("camera", restarted.ref.subject_id)
    latest = timeline.samples.latest("camera", restarted.ref.subject_id)
    assert latest is not None
    assert latest.record.generation == 4


async def test_repeated_sample_does_not_unbind_the_frame_of_the_admitted_one() -> None:
    sensed, timeline, journal = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            samples=[
                sample("stream-screen", "screen", 1, generation=1),
                sample("stream-screen", "screen", 1, generation=1),
            ],
            frames=[generated("stream-screen", "screen", ROADMAP, 1, 1)],
        )
    )

    assert timeline.snapshot().current_screen is not None
    assert journal.events("perception_frame_dropped") == []
