import base64
from datetime import UTC, datetime, timedelta
from typing import Any

from fakeyou.config.settings import PerceptionSettings
from fakeyou.perception.sensors import (
    PERCEPTION_ASSIGN_JS,
    PERCEPTION_DRAIN_JS,
    DrainBatch,
    DrainedFrame,
    DrainedMessage,
    PerceptionDrain,
    PerceptionSensors,
    load_perception_script,
)
from fakeyou.perception.timeline import MeetingTimeline
from tests._meet_protocol_fakes import (
    chat_message,
    collection_payload,
    output,
    sync_body,
    user_info,
)
from tests._perception_fakes import slide_jpeg
from tests._persistence_fakes import RecordingJournal

JOINED = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
JOINED_MS = JOINED.timestamp() * 1000
AYU = "spaces/s/devices/ayu"
BUDI = "spaces/s/devices/budi"
TWIN = "spaces/s/devices/twin"
SCREEN = "spaces/s/devices/ayu-screen"


def message(payload: bytes, *, at_ms: float = JOINED_MS + 1000) -> DrainedMessage:
    return DrainedMessage(
        channel="collections", page_ms=at_ms, b64=base64.b64encode(payload).decode()
    )


def frame(
    stream_id: str, role: str, data: bytes, *, at_ms: float = JOINED_MS + 5000
) -> DrainedFrame:
    return DrainedFrame.model_validate(
        {
            "stream_id": stream_id,
            "role": role,
            "page_ms": at_ms,
            "width": 1280,
            "height": 720,
            "reason": "settled",
            "jpeg_b64": base64.b64encode(data).decode(),
        }
    )


def sensors(**settings: Any) -> tuple[PerceptionSensors, MeetingTimeline, RecordingJournal]:
    journal = RecordingJournal()
    timeline = MeetingTimeline(journal=journal)
    sensed = PerceptionSensors(
        timeline,
        PerceptionSettings(enabled=True, **settings),
        joined_at=JOINED,
        journal=journal,
        clock=lambda: JOINED + timedelta(minutes=1),
    )
    return sensed, timeline, journal


def people() -> bytes:
    return collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Rani Twin", current=True),
            user_info(SCREEN, "", parent=AYU),
        ],
        outputs=[output(SCREEN, "stream-screen"), output(BUDI, "stream-budi")],
    )


async def test_a_presenter_and_their_screen_reach_the_timeline() -> None:
    sensed, timeline, _ = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[frame("stream-screen", "screen", slide_jpeg("Launch Timeline"))],
        )
    )
    snapshot = timeline.snapshot()

    assert snapshot.presenting is not None
    assert snapshot.presenting.name == "Ayu"
    assert snapshot.current_screen is not None
    assert snapshot.current_screen.presenter == snapshot.presenting
    assert {person.name for person in snapshot.participants} == {"Ayu", "Budi"}
    assert [signal.kind for signal in snapshot.signals].count("presenting_started") == 1


async def test_camera_frames_are_kept_per_participant_not_as_screens() -> None:
    sensed, timeline, _ = sensors()
    data = slide_jpeg("Budi holding a mockup")

    await sensed.accept_batch(
        DrainBatch(messages=[message(people())], frames=[frame("stream-budi", "camera", data)])
    )

    assert timeline.snapshot().screens == ()
    assert timeline.camera_frames.get(BUDI) == data


async def test_frames_from_unassigned_or_mismatched_streams_are_ignored() -> None:
    sensed, timeline, _ = sensors()
    data = slide_jpeg("Launch Timeline")

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(people())],
            frames=[frame("stream-unknown", "screen", data), frame("stream-budi", "screen", data)],
        )
    )

    assert timeline.snapshot().screens == ()
    assert sensed.frames_seen == 0


async def test_assignments_follow_the_perception_toggles() -> None:
    both, _, _ = sensors()
    screen_only, _, _ = sensors(cameras=False)
    for sensed in (both, screen_only):
        await sensed.accept_batch(DrainBatch(messages=[message(people())]))

    assert {item.role for item in both.assignments()} == {"screen", "camera"}
    assert {item.role for item in screen_only.assignments()} == {"screen"}


async def test_chat_is_attributed_and_marks_own_and_backlog_lines() -> None:
    sensed, timeline, _ = sensors()
    before_join = int((JOINED_MS - 60_000) * 1000)
    after_join = int((JOINED_MS + 30_000) * 1000)
    payload = collection_payload(
        users=[user_info(BUDI, "Budi"), user_info(TWIN, "Rani Twin", current=True)],
        chat=[
            chat_message("m1", BUDI, "old question", before_join),
            chat_message("m2", BUDI, "deck link?", after_join),
            chat_message("m3", TWIN, "Here it is", after_join),
        ],
    )

    await sensed.accept_batch(DrainBatch(messages=[message(payload)]))
    lines = {line.message_id: line for line in timeline.snapshot().chat}

    assert (lines["m1"].backlog, lines["m2"].backlog) == (True, False)
    assert (lines["m2"].own, lines["m3"].own) == (False, True)
    assert lines["m2"].sender.name == "Budi"


async def test_chat_is_ignored_when_switched_off() -> None:
    sensed, timeline, _ = sensors(chat=False)
    payload = collection_payload(chat=[chat_message("m1", BUDI, "hello", int(JOINED_MS))])

    await sensed.accept_batch(DrainBatch(messages=[message(payload)]))

    assert timeline.snapshot().chat == ()


async def test_join_and_leave_signals_respect_the_signals_toggle() -> None:
    quiet, quiet_timeline, _ = sensors(signals=False)
    loud, loud_timeline, _ = sensors()
    for sensed in (quiet, loud):
        await sensed.accept_batch(DrainBatch(messages=[message(people())]))
        sensed.accept_sync(sync_body([user_info(BUDI, "Budi", status=2)]))

    assert [signal.kind for signal in quiet_timeline.snapshot().signals] == ["presenting_started"]
    assert "left" in [signal.kind for signal in loud_timeline.snapshot().signals]


async def test_an_empty_sync_keeps_the_presenter_and_the_roster() -> None:
    sensed, timeline, _ = sensors()
    await sensed.accept_batch(DrainBatch(messages=[message(people())]))

    sensed.accept_sync(sync_body([]))

    snapshot = timeline.snapshot()
    assert snapshot.presenting is not None
    assert "left" not in [signal.kind for signal in snapshot.signals]
    assert {item.role for item in sensed.assignments()} == {"screen", "camera"}


async def test_unreadable_inputs_are_counted_and_journaled() -> None:
    sensed, _, journal = sensors()

    await sensed.accept_batch(
        DrainBatch(
            messages=[message(b"garbage")],
            frames=[frame("stream-screen", "screen", b"not a jpeg")],
        )
    )
    sensed.accept_sync(b"%%%")

    kinds = [row["kind"] for row in journal.events("perception_input_rejected")]
    assert kinds == ["collection", "sync"]


class FakePerceptionPage:
    def __init__(self, batches: list[Any]) -> None:
        self.batches = batches
        self.assigned: list[Any] = []

    async def evaluate(self, expression: str, arg: Any = None, /) -> Any:
        if expression == PERCEPTION_DRAIN_JS:
            return self.batches.pop(0) if self.batches else None
        if expression == PERCEPTION_ASSIGN_JS:
            self.assigned.append(arg)
            return {"sampling": [item["stream_id"] for item in arg]}
        raise AssertionError(expression)


class FakeResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.url = url
        self._body = body

    async def body(self) -> bytes:
        return self._body


async def test_the_drain_feeds_sensors_and_assigns_streams_once() -> None:
    sensed, timeline, _ = sensors()
    raw = DrainBatch(messages=[message(people())]).model_dump()
    page = FakePerceptionPage([raw, {"messages": []}, {"unexpected": True, "frames": "bad"}])
    drain = PerceptionDrain(page, sensed)

    await drain.drain_once()
    await drain.drain_once()
    await drain.drain_once()

    assert len(page.assigned) == 1
    assert {item["role"] for item in page.assigned[0]} == {"screen", "camera"}
    assert drain.batches_rejected == 1
    assert timeline.snapshot().presenting is not None


async def test_sync_responses_update_the_roster_and_are_journaled() -> None:
    sensed, timeline, journal = sensors()

    await sensed.on_response(FakeResponse("https://example.test/other", b""))
    await sensed.on_response(
        FakeResponse(
            "https://meet.google.com/$rpc/google.rtc.meetings.v1.MeetingSpaceService/"
            "SyncMeetingSpaceCollections",
            sync_body([user_info(AYU, "Ayu"), user_info(SCREEN, "", parent=AYU)]),
        )
    )

    assert [person.name for person in timeline.snapshot().participants] == ["Ayu"]
    [synced] = journal.events("perception_sync_accepted")
    assert (synced["users"], synced["screens"]) == (2, 1)


def test_the_perception_script_ships_with_the_bridge() -> None:
    script = load_perception_script()

    assert script.index("__fakeyouRtc") < script.index("__fakeyouPerception")


async def test_each_audio_output_is_journaled_once_with_its_device() -> None:
    sensed, _, journal = sensors()
    audio = collection_payload(
        users=[user_info(AYU, "Ayu")],
        outputs=[output(AYU, "3553701704", kind=1), output(AYU, "stream-ayu")],
    )

    await sensed.accept_batch(DrainBatch(messages=[message(audio), message(audio)]))

    assert [
        (row["device_id"], row["stream_id"], row["own"])
        for row in journal.events("perception_audio_output_observed")
    ] == [(AYU, "3553701704", False)]
