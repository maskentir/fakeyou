import asyncio
import base64
import io
import json
import threading
import time
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from fakeyou.config.settings import PerceptionSettings
from fakeyou.observability.journal import Journal
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.perception import context as context_module
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.camera_evidence import StaleEvidence
from fakeyou.perception.context import (
    CAMERA_LIVENESS_TIMEOUT_SECONDS,
    MeetingEvidence,
    PerceptionContext,
)
from fakeyou.perception.frames import keyframe_from_jpeg
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.percepts import Keyframe
from fakeyou.perception.sensors import (
    ActiveSampler,
    DrainBatch,
    DrainedFrame,
    DrainedMessage,
    PerceptionSensors,
)
from fakeyou.perception.timeline import (
    CameraFrame,
    CameraFrameStore,
    CameraSnapshot,
    MeetingTimeline,
)
from tests._meet_protocol_fakes import collection_payload, output, user_info
from tests._perception_fakes import FakeVisionModel, vision_model
from tests._persistence_fakes import RecordingJournal

JOINED = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
JOINED_MS = JOINED.timestamp() * 1000
AYU = "spaces/s/devices/ayu"
BUDI = "spaces/s/devices/budi"
TWIN = "spaces/s/devices/twin"


QUESTION = "What color is the shirt?"
SOURCE = "the live camera of the participant Budi, so the person shown is Budi"


def interpreter_with(
    model: FakeVisionModel,
    store: CameraFrameStore,
    *,
    journal: RecordingJournal | None = None,
    max_calls: int = 50,
) -> PerceptionInterpreter:
    return PerceptionInterpreter(
        model,
        PerceptionBudget(budget_usd=10.0, max_calls=max_calls),
        max_concurrent=2,
        snapshots=store,
        journal=journal,
    )


def camera_stack(
    model: FakeVisionModel,
    *,
    journal: RecordingJournal | None = None,
    max_calls: int = 50,
) -> tuple[MeetingTimeline, PerceptionContext, PerceptionInterpreter]:
    timeline = MeetingTimeline()
    timeline.participant("device-p1", "Budi")
    interpreter = interpreter_with(
        model, timeline.camera_frames, journal=journal, max_calls=max_calls
    )
    context = PerceptionContext(timeline, interpreter, max_chars=6000)
    return timeline, context, interpreter


def put_frame(
    timeline: MeetingTimeline, data: bytes, *, stream_id: str = "stream-p1"
) -> CameraSnapshot:
    timeline.camera_frames.put(
        "device-p1",
        CameraFrame(data, stream_id, datetime.now(UTC), 0, time.monotonic()),
    )
    snapshot = timeline.camera_frames.snapshot("device-p1")
    assert snapshot is not None
    return snapshot


def jpeg_of(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def test_the_same_question_on_the_same_frame_is_answered_once() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((200, 180, 160)))

    first = await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)
    second = await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)

    assert first is not None
    assert first.resolution == "fresh"
    assert first.answer == "A dark blue shirt."
    assert second is not None
    assert second.resolution == "cache_hit"
    assert second.vision_latency_ms == 0
    assert len(model.seen) == 1


async def test_a_different_frame_is_inspected_again() -> None:
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    timeline, _context, interpreter = camera_stack(model)

    await interpreter.camera_answer(
        "device-p1", QUESTION, put_frame(timeline, jpeg_of((10, 10, 10))), source=SOURCE
    )
    second = await interpreter.camera_answer(
        "device-p1", QUESTION, put_frame(timeline, jpeg_of((250, 250, 250))), source=SOURCE
    )

    assert second is not None
    assert second.resolution == "fresh"
    assert second.answer == "A red shirt."
    assert len(model.seen) == 2


async def test_another_participant_is_answered_separately() -> None:
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((120, 120, 120)))
    timeline.participant("device-p2", "Ayu")
    timeline.camera_frames.put(
        "device-p2",
        CameraFrame(jpeg_of((120, 120, 120)), "stream-p2", datetime.now(UTC), 0, time.monotonic()),
    )
    other_snapshot = timeline.camera_frames.snapshot("device-p2")
    assert other_snapshot is not None

    await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)
    other = await interpreter.camera_answer("device-p2", QUESTION, other_snapshot, source=SOURCE)

    assert other is not None
    assert other.resolution == "fresh"
    assert len(model.seen) == 2


async def test_two_identical_questions_about_one_frame_share_one_vision_call() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((90, 90, 90)))

    first, second = await asyncio.gather(
        interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE),
        interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE),
    )

    assert first is not None
    assert second is not None
    assert {first.resolution, second.resolution} == {"cache_hit", "fresh"}
    assert len(model.seen) == 1


async def test_two_different_questions_about_one_frame_get_their_own_answers() -> None:
    model = vision_model(answers=["A dark blue shirt.", "The shirt says HELLO."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((90, 90, 90)))

    first = await interpreter.camera_answer(
        "device-p1", "What color is the shirt?", snapshot, source=SOURCE
    )
    second = await interpreter.camera_answer(
        "device-p1", "What text is on the shirt?", snapshot, source=SOURCE
    )

    assert first is not None
    assert second is not None
    assert first.answer == "A dark blue shirt."
    assert second.answer == "The shirt says HELLO."
    assert second.resolution == "fresh"
    assert len(model.seen) == 2


async def test_a_stale_answer_is_not_reused() -> None:
    clock = [0.0]
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    timeline, _context, interpreter = camera_stack(model)
    object.__setattr__(interpreter, "_monotonic", lambda: clock[0])
    snapshot = put_frame(timeline, jpeg_of((60, 60, 60)))

    await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)
    clock[0] = 3600.0
    later = await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)

    assert later is not None
    assert later.resolution == "fresh"
    assert later.answer == "A red shirt."


async def test_resolving_evidence_is_journalled_with_its_resolution() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    journal = RecordingJournal()
    timeline, context, interpreter = camera_stack(model, journal=journal)
    put_frame(timeline, jpeg_of((140, 130, 120)))
    evidence = MeetingEvidence(context, journal=journal)

    await evidence("what colour is the shirt?", ("participant",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["evidence_kind"] == "participant"
    assert resolved["resolution"] == "fresh"
    assert resolved["transcript_id"] == "t1"
    assert resolved["vision_latency_ms"] >= 0
    assert resolved["strategy"] == "targeted_vision"


async def test_a_camera_question_is_answered_by_one_inspection_of_the_live_frame() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, _interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "ok"
    assert result["answer"] == "A dark blue shirt."
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "targeted_vision"
    assert result["vision_latency_ms"] >= 0
    assert len(model.seen) == 1


async def test_the_same_camera_question_is_answered_from_the_cache() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, _interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    await context.look_at_participant("Budi", "What color is his shirt?")
    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "cache_hit"
    assert result["vision_latency_ms"] == 0
    assert len(model.seen) == 1


async def test_another_camera_question_is_inspected_again() -> None:
    model = vision_model(answers=["A dark blue shirt.", "The shirt says HELLO."])
    timeline, context, _interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    await context.look_at_participant("Budi", "What color is his shirt?")
    result = json.loads(await context.look_at_participant("Budi", "What text is on the shirt?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
    assert result["answer"] == "The shirt says HELLO."
    assert len(model.seen) == 2


async def test_a_camera_question_the_model_cannot_see_keeps_the_model_answer() -> None:
    model = vision_model(answers=["The text is not visible in the frame."])
    timeline, context, _interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    result = json.loads(await context.look_at_participant("Budi", "What text is on his shirt?"))

    assert result["code"] == "ok"
    assert result["answer"] == "The text is not visible in the frame."
    assert result["strategy"] == "targeted_vision"
    assert len(model.seen) == 1


async def test_budget_exhausted_for_camera_does_not_guess() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, _interpreter = camera_stack(model, max_calls=0)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "budget_exhausted"
    assert result["strategy"] == "targeted_vision"


async def test_participant_resolution_journals_the_strategy() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    journal = RecordingJournal()
    timeline, context, _interpreter = camera_stack(model, journal=journal)
    put_frame(timeline, jpeg_of((140, 130, 120)))
    evidence = MeetingEvidence(context, journal=journal)

    await evidence("What color is his shirt?", ("participant",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["strategy"] == "targeted_vision"


async def test_an_invalidation_before_the_inference_rejects_the_answer() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((120, 120, 120)))
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        timeline.camera_frames.drop("device-p1")
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    with pytest.raises(StaleEvidence):
        await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)

    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("device-p1") is None


async def test_an_invalidation_during_the_preprocessing_rejects_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((120, 120, 120)))
    started = threading.Event()
    release = threading.Event()
    original = keyframe_from_jpeg

    def slow_keyframe(data: bytes, *, captured_at: datetime) -> Keyframe:
        started.set()
        release.wait(2)
        return original(data, captured_at=captured_at)

    monkeypatch.setattr("fakeyou.perception.interpreter.keyframe_from_jpeg", slow_keyframe)

    task = asyncio.create_task(
        interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)
    )
    await asyncio.sleep(0.05)
    assert started.wait(2)
    timeline.camera_frames.drop("device-p1")
    release.set()

    with pytest.raises(StaleEvidence):
        await task

    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("device-p1") is None


async def test_a_new_frame_during_the_inference_rejects_the_answer() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((120, 120, 120)))
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        timeline.camera_frames.put(
            "device-p1",
            CameraFrame(
                jpeg_of((200, 200, 200)), "stream-p1", datetime.now(UTC), 0, time.monotonic()
            ),
        )
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    with pytest.raises(StaleEvidence):
        await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)

    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("device-p1") is None


async def test_a_cache_hit_is_refused_when_the_frame_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, _context, interpreter = camera_stack(model)
    snapshot = put_frame(timeline, jpeg_of((120, 120, 120)))
    first = await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)
    assert first is not None
    original = keyframe_from_jpeg

    def replaced(data: bytes, *, captured_at: datetime) -> Keyframe:
        timeline.camera_frames.put(
            "device-p1",
            CameraFrame(
                jpeg_of((200, 200, 200)), "stream-p1", datetime.now(UTC), 0, time.monotonic()
            ),
        )
        return original(data, captured_at=captured_at)

    monkeypatch.setattr("fakeyou.perception.interpreter.keyframe_from_jpeg", replaced)

    with pytest.raises(StaleEvidence):
        await interpreter.camera_answer("device-p1", QUESTION, snapshot, source=SOURCE)

    assert len(model.seen) == 1
    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("device-p1") is not None


async def test_a_provider_failure_reports_a_failed_look() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    async def failing(_image: bytes, _question: str, **_kwargs: Any) -> str | None:
        raise ProviderFault("perception", "unknown")

    object.__setattr__(interpreter, "answer", failing)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "look_failed"
    assert result["error_type"] == "ProviderFault"
    assert result["resolution"] == "unavailable"
    assert result["strategy"] == "targeted_vision"


async def test_a_hanging_provider_times_out_the_camera_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))
    monkeypatch.setattr(context_module, "LOOK_TIMEOUT_SECONDS", 0.05)

    async def hanging(_image: bytes, _question: str, **_kwargs: Any) -> str | None:
        await asyncio.sleep(30)
        return "never"

    object.__setattr__(interpreter, "answer", hanging)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "look_failed"
    assert result["error_type"] == "TimeoutError"
    assert result["resolution"] == "unavailable"


async def test_a_cancelled_camera_look_propagates_the_cancellation() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    timeline, context, interpreter = camera_stack(model)
    put_frame(timeline, jpeg_of((140, 130, 120)))

    async def hanging(_image: bytes, _question: str, **_kwargs: Any) -> str | None:
        await asyncio.sleep(30)
        return "never"

    object.__setattr__(interpreter, "answer", hanging)

    task = asyncio.create_task(context.look_at_participant("Budi", "What color is his shirt?"))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def liveness_jpeg(color: tuple[int, int, int]) -> bytes:
    import io

    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def liveness_frame(stream_id: str, data: bytes, *, at_ms: float = JOINED_MS + 5000) -> DrainedFrame:

    return DrainedFrame.model_validate(
        {
            "stream_id": stream_id,
            "role": "camera",
            "page_ms": at_ms,
            "width": 64,
            "height": 48,
            "reason": "changed",
            "jpeg_b64": base64.b64encode(data).decode(),
        }
    )


def liveness_people(*, budi_camera: bool = True) -> bytes:
    outputs = [output(BUDI, "stream-budi", disabled=not budi_camera)]
    return collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=outputs,
    )


class CameraStack:
    def __init__(self) -> None:
        self.clock = [0.0]
        self.sample_ms = JOINED_MS
        self.journal = Journal("m1", emit=lambda _: None)
        self.timeline = MeetingTimeline(journal=self.journal)
        self.model = vision_model()
        self.interpreter = PerceptionInterpreter(
            self.model,
            PerceptionBudget(budget_usd=10.0, max_calls=50),
            max_concurrent=2,
            snapshots=self.timeline.camera_frames,
            journal=self.journal,
            monotonic=lambda: self.clock[0],
        )
        self.sensors = PerceptionSensors(
            self.timeline,
            PerceptionSettings(enabled=True),
            joined_at=JOINED,
            journal=self.journal,
            on_camera_invalidate=self._invalidate,
        )
        self.sensors.monotonic = lambda: self.clock[0]
        self.context = PerceptionContext(
            self.timeline,
            self.interpreter,
            max_chars=6000,
            state=self.sensors.roster.state,
            camera_epoch=lambda: self.sensors.epoch,
            camera_monotonic=lambda: self.clock[0],
        )

    def _invalidate(self, participant_id: str | None) -> None:
        if participant_id is None:
            self.interpreter.clear_camera_observations()
        else:
            self.interpreter.drop_camera_observation(participant_id)

    async def setup_camera(self, data: bytes) -> None:
        await self.sensors.accept_batch(DrainBatch(messages=[]))

        msg = DrainedMessage(
            channel="collections",
            page_ms=JOINED_MS + 1000,
            b64=base64.b64encode(liveness_people()).decode(),
        )
        await self.sensors.accept_batch(
            DrainBatch(messages=[msg], frames=[liveness_frame("stream-budi", data)])
        )

    async def heartbeat(self, *, advance: bool = True) -> None:
        if advance:
            self.sample_ms += 4000
        await self.sensors.accept_batch(
            DrainBatch(
                active=[
                    ActiveSampler(
                        stream_id="stream-budi",
                        role="camera",
                        last_sample_page_ms=self.sample_ms,
                    )
                ]
            )
        )

    async def look(self) -> dict[str, Any]:
        import json

        raw = await self.context.look_at_participant("Budi", "what are they wearing?")
        return cast(dict[str, Any], json.loads(raw))


async def test_camera_off_makes_evidence_unavailable_and_invalidates_cache() -> None:
    stack = CameraStack()
    data = liveness_jpeg((200, 180, 160))
    await stack.setup_camera(data)
    await stack.heartbeat()

    first = await stack.look()
    assert first["code"] == "ok"

    off = DrainedMessage(
        channel="collections",
        page_ms=JOINED_MS + 3000,
        b64=base64.b64encode(liveness_people(budi_camera=False)).decode(),
    )
    await stack.sensors.accept_batch(DrainBatch(messages=[off]))

    unavailable = await stack.look()
    assert unavailable["code"] == "camera_unavailable"
    assert stack.timeline.camera_frames.get(BUDI) is None

    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 4000,
                    b64=base64.b64encode(liveness_people(budi_camera=True)).decode(),
                )
            ],
            frames=[liveness_frame("stream-budi", data, at_ms=JOINED_MS + 4000)],
        )
    )
    await stack.heartbeat()

    fresh = await stack.look()
    assert fresh["code"] == "ok"
    assert fresh["resolution"] == "fresh"


async def test_participant_leave_invalidates_frame_and_cache() -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    leave = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi", status=2),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[],
    )

    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 3000,
                    b64=base64.b64encode(leave).decode(),
                )
            ]
        )
    )

    result = await stack.look()
    assert result["code"] in {"camera_unavailable", "unknown_participant"}
    assert stack.timeline.camera_frames.get(BUDI) is None
    assert BUDI not in vars(stack.interpreter)["_camera_cache"].entries


async def test_rejoin_epoch_rejects_previous_frame() -> None:
    stack = CameraStack()
    data = liveness_jpeg((90, 90, 90))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    stack.sensors.begin_epoch()
    unavailable = await stack.look()
    assert unavailable["code"] == "camera_unavailable"

    await stack.setup_camera(data)
    await stack.heartbeat()
    fresh = await stack.look()
    assert fresh["code"] == "ok"
    assert fresh["resolution"] == "fresh"


async def test_a_stable_camera_stays_live_with_heartbeats() -> None:
    stack = CameraStack()
    data = liveness_jpeg((60, 70, 80))
    await stack.setup_camera(data)

    for step in range(20):
        stack.clock[0] = step * 4.5
        await stack.heartbeat()
    stack.clock[0] = 90.0
    await stack.heartbeat()

    result = await stack.look()
    assert result["code"] == "ok"

    stack.clock[0] = 90.0 + 61.0
    await stack.heartbeat()
    later = await stack.look()
    assert later["code"] == "ok"
    assert later["resolution"] == "fresh"


async def test_a_stalled_sampling_stream_becomes_unavailable() -> None:
    stack = CameraStack()
    data = liveness_jpeg((30, 40, 50))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    for step in range(6):
        stack.clock[0] = step * 4.5
        await stack.heartbeat(advance=False)

    result = await stack.look()
    assert result["code"] == "camera_unavailable"


async def test_a_stream_without_heartbeats_becomes_unavailable() -> None:
    stack = CameraStack()
    data = liveness_jpeg((30, 40, 50))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    stack.clock[0] = CAMERA_LIVENESS_TIMEOUT_SECONDS + 1.0
    result = await stack.look()
    assert result["code"] == "camera_unavailable"


def slow_decoder(monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    started = threading.Event()
    release = threading.Event()
    original = keyframe_from_jpeg

    def slow_keyframe(data: bytes, *, captured_at: datetime) -> Keyframe:
        started.set()
        release.wait(2)
        return original(data, captured_at=captured_at)

    monkeypatch.setattr("fakeyou.perception.sensors.keyframe_from_jpeg", slow_keyframe)
    return started, release


async def _frame_in_flight(
    stack: CameraStack, monkeypatch: pytest.MonkeyPatch, data: bytes
) -> tuple[asyncio.Task[None], threading.Event]:
    started, release = slow_decoder(monkeypatch)
    task = asyncio.create_task(
        stack.sensors.accept_batch(
            DrainBatch(frames=[liveness_frame("stream-budi", data, at_ms=JOINED_MS + 5000)])
        )
    )
    await asyncio.sleep(0.05)
    assert started.wait(2)
    return task, release


async def test_a_frame_from_the_previous_epoch_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    task, release = await _frame_in_flight(stack, monkeypatch, data)

    stack.sensors.begin_epoch()
    release.set()
    await task

    assert stack.timeline.camera_frames.get(BUDI) is None


async def test_a_frame_from_a_camera_that_went_off_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    task, release = await _frame_in_flight(stack, monkeypatch, data)

    off = DrainedMessage(
        channel="collections",
        page_ms=JOINED_MS + 3000,
        b64=base64.b64encode(liveness_people(budi_camera=False)).decode(),
    )
    await stack.sensors.accept_batch(DrainBatch(messages=[off]))
    release.set()
    await task

    assert stack.timeline.camera_frames.get(BUDI) is None


async def test_a_frame_from_a_participant_that_left_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    task, release = await _frame_in_flight(stack, monkeypatch, data)

    leave = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi", status=2),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[],
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 3000,
                    b64=base64.b64encode(leave).decode(),
                )
            ]
        )
    )
    release.set()
    await task

    assert stack.timeline.camera_frames.get(BUDI) is None


async def test_a_frame_from_a_reassigned_stream_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = CameraStack()
    data = liveness_jpeg((120, 120, 120))
    await stack.setup_camera(data)
    task, release = await _frame_in_flight(stack, monkeypatch, data)

    rebound = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[output(BUDI, "stream-budi-2")],
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 3000,
                    b64=base64.b64encode(rebound).decode(),
                )
            ]
        )
    )
    release.set()
    await task

    frame = stack.timeline.camera_frames.get(BUDI)
    assert frame is not None
    assert frame.sequence == 1
    assert frame.stream_id == "stream-budi"


async def test_a_new_stream_replaces_the_old_observation() -> None:
    stack = CameraStack()
    data = liveness_jpeg((10, 20, 30))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    rebound = collection_payload(
        users=[
            user_info(AYU, "Ayu"),
            user_info(BUDI, "Budi"),
            user_info(TWIN, "Twin", current=True),
        ],
        outputs=[output(BUDI, "stream-budi-2")],
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            messages=[
                DrainedMessage(
                    channel="collections",
                    page_ms=JOINED_MS + 6100,
                    b64=base64.b64encode(rebound).decode(),
                )
            ],
            frames=[
                liveness_frame(
                    "stream-budi-2", liveness_jpeg((200, 210, 220)), at_ms=JOINED_MS + 6100
                )
            ],
        )
    )
    await stack.sensors.accept_batch(
        DrainBatch(
            active=[
                ActiveSampler(
                    stream_id="stream-budi-2",
                    role="camera",
                    last_sample_page_ms=JOINED_MS + 6200,
                )
            ]
        )
    )

    result = await stack.look()
    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
