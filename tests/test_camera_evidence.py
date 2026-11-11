import asyncio
import base64
import io
import json
import time
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from fakeyou.config.settings import PerceptionSettings
from fakeyou.observability.journal import Journal
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.camera_evidence import StaleEvidence
from fakeyou.perception.context import (
    CAMERA_LIVENESS_TIMEOUT_SECONDS,
    MeetingEvidence,
    PerceptionContext,
)
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.sensors import (
    ActiveSampler,
    DrainBatch,
    DrainedFrame,
    DrainedMessage,
    PerceptionSensors,
)
from fakeyou.perception.timeline import CameraFrame, MeetingTimeline
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
    model: FakeVisionModel, *, journal: RecordingJournal | None = None
) -> PerceptionInterpreter:
    return PerceptionInterpreter(
        model,
        PerceptionBudget(budget_usd=10.0, max_calls=50),
        max_concurrent=2,
        journal=journal,
    )


def _context_with(interpreter: PerceptionInterpreter) -> PerceptionContext:
    timeline = MeetingTimeline()
    timeline.participant("device-p1", "Budi")
    timeline.camera_frames.put(
        "device-p1",
        CameraFrame(jpeg_of((140, 130, 120)), "stream-p1", datetime.now(UTC), 0, time.monotonic()),
    )
    return PerceptionContext(timeline, interpreter, max_chars=6000)


def jpeg_of(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def test_the_same_question_on_the_same_frame_is_answered_once() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((200, 180, 160))

    first = await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)
    second = await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)

    assert first is not None
    assert first.resolution == "fresh"
    assert first.answer == "A dark blue shirt."
    assert second is not None
    assert second.resolution == "cache_hit"
    assert second.vision_latency_ms == 0
    assert len(model.seen) == 1


async def test_a_different_frame_is_inspected_again() -> None:
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    interpreter = interpreter_with(model)

    await interpreter.camera_answer("p1", QUESTION, jpeg_of((10, 10, 10)), source=SOURCE)
    second = await interpreter.camera_answer(
        "p1", QUESTION, jpeg_of((250, 250, 250)), source=SOURCE
    )

    assert second is not None
    assert second.resolution == "fresh"
    assert second.answer == "A red shirt."
    assert len(model.seen) == 2


async def test_another_participant_is_answered_separately() -> None:
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))

    await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)
    other = await interpreter.camera_answer("p2", QUESTION, frame, source=SOURCE)

    assert other is not None
    assert other.resolution == "fresh"
    assert len(model.seen) == 2


async def test_two_identical_questions_about_one_frame_share_one_vision_call() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((90, 90, 90))

    first, second = await asyncio.gather(
        interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE),
        interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE),
    )

    assert first is not None
    assert second is not None
    assert {first.resolution, second.resolution} == {"cache_hit", "fresh"}
    assert len(model.seen) == 1


async def test_two_different_questions_about_one_frame_get_their_own_answers() -> None:
    model = vision_model(answers=["A dark blue shirt.", "The shirt says HELLO."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((90, 90, 90))

    first = await interpreter.camera_answer("p1", "What color is the shirt?", frame, source=SOURCE)
    second = await interpreter.camera_answer(
        "p1", "What text is on the shirt?", frame, source=SOURCE
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
    interpreter = interpreter_with(model)
    object.__setattr__(interpreter, "_monotonic", lambda: clock[0])
    frame = jpeg_of((60, 60, 60))

    await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)
    clock[0] = 3600.0
    later = await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)

    assert later is not None
    assert later.resolution == "fresh"
    assert later.answer == "A red shirt."


async def test_resolving_evidence_is_journalled_with_its_resolution() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    journal = RecordingJournal()
    context = _context_with(interpreter)
    evidence = MeetingEvidence(context, journal=journal)

    await evidence("what colour is the shirt?", ("participant",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["evidence_kind"] == "participant"
    assert resolved["resolution"] == "fresh"
    assert resolved["transcript_id"] == "t1"
    assert resolved["vision_latency_ms"] >= 0
    assert resolved["strategy"] == "targeted_vision"


def live_context(
    model: FakeVisionModel, *, max_calls: int = 10, journal: RecordingJournal | None = None
) -> PerceptionContext:
    timeline = MeetingTimeline()
    timeline.participant("device-p1", "Budi")
    timeline.camera_frames.put(
        "device-p1",
        CameraFrame(jpeg_of((140, 130, 120)), "stream-p1", datetime.now(UTC), 0, time.monotonic()),
    )
    interpreter = PerceptionInterpreter(
        model,
        PerceptionBudget(budget_usd=10.0, max_calls=max_calls),
        max_concurrent=2,
        journal=journal,
    )
    return PerceptionContext(timeline, interpreter, max_chars=6000)


async def test_a_camera_question_is_answered_by_one_inspection_of_the_live_frame() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "ok"
    assert result["answer"] == "A dark blue shirt."
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "targeted_vision"
    assert result["vision_latency_ms"] >= 0
    assert len(model.seen) == 1


async def test_the_same_camera_question_is_answered_from_the_cache() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    context = live_context(model)

    await context.look_at_participant("Budi", "What color is his shirt?")
    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "cache_hit"
    assert result["vision_latency_ms"] == 0
    assert len(model.seen) == 1


async def test_another_camera_question_is_inspected_again() -> None:
    model = vision_model(answers=["A dark blue shirt.", "The shirt says HELLO."])
    context = live_context(model)

    await context.look_at_participant("Budi", "What color is his shirt?")
    result = json.loads(await context.look_at_participant("Budi", "What text is on the shirt?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
    assert result["answer"] == "The shirt says HELLO."
    assert len(model.seen) == 2


async def test_a_camera_question_the_model_cannot_see_keeps_the_model_answer() -> None:
    model = vision_model(answers=["The text is not visible in the frame."])
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What text is on his shirt?"))

    assert result["code"] == "ok"
    assert result["answer"] == "The text is not visible in the frame."
    assert result["strategy"] == "targeted_vision"
    assert len(model.seen) == 1


async def test_budget_exhausted_for_camera_does_not_guess() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    context = live_context(model, max_calls=0)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "budget_exhausted"
    assert result["strategy"] == "targeted_vision"


async def test_an_inference_that_finishes_after_an_invalidation_is_not_published() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))
    started = asyncio.Event()
    release = asyncio.Event()
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        started.set()
        await release.wait()
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    task = asyncio.create_task(interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE))
    await started.wait()
    interpreter.drop_camera_observation("p1")
    release.set()

    with pytest.raises(StaleEvidence):
        await task
    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("p1", QUESTION) is None


async def test_an_inference_that_finishes_after_a_rejoin_is_not_published() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))
    started = asyncio.Event()
    release = asyncio.Event()
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        started.set()
        await release.wait()
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    task = asyncio.create_task(interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE))
    await started.wait()
    interpreter.clear_camera_observations()
    release.set()

    with pytest.raises(StaleEvidence):
        await task
    cache = object.__getattribute__(interpreter, "_camera_cache")
    assert cache.get("p1", QUESTION) is None


async def test_a_rejected_inference_leaves_the_cache_ready_for_a_new_look() -> None:
    model = vision_model(answers=["A dark blue shirt.", "A red shirt."])
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))
    started = asyncio.Event()
    release = asyncio.Event()
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        started.set()
        await release.wait()
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    task = asyncio.create_task(interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE))
    await started.wait()
    interpreter.drop_camera_observation("p1")
    release.set()

    with pytest.raises(StaleEvidence):
        await task

    again = await interpreter.camera_answer("p1", QUESTION, frame, source=SOURCE)

    assert again is not None
    assert again.resolution == "fresh"
    assert again.answer == "A red shirt."


async def test_a_stale_camera_look_reports_the_camera_as_unavailable() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    interpreter = interpreter_with(model)
    context = _context_with(interpreter)
    original = interpreter.answer

    async def answered(image: bytes, question: str, *, source: str) -> str | None:
        interpreter.drop_camera_observation("device-p1")
        return await original(image, question, source=source)

    object.__setattr__(interpreter, "answer", answered)

    result = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))

    assert result["code"] == "camera_unavailable"
    assert result["resolution"] == "unavailable"


async def test_participant_resolution_journals_the_strategy() -> None:
    model = vision_model(answers=["A dark blue shirt."])
    journal = RecordingJournal()
    context = live_context(model, journal=journal)
    evidence = MeetingEvidence(context, journal=journal)

    await evidence("What color is his shirt?", ("participant",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["strategy"] == "targeted_vision"


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
        self.journal = Journal("m1", emit=lambda _: None)
        self.timeline = MeetingTimeline(journal=self.journal)
        self.model = vision_model()
        self.interpreter = PerceptionInterpreter(
            self.model,
            PerceptionBudget(budget_usd=10.0, max_calls=50),
            max_concurrent=2,
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

    async def heartbeat(self) -> None:
        await self.sensors.accept_batch(
            DrainBatch(
                active=[
                    ActiveSampler(stream_id="stream-budi", role="camera", page_ms=JOINED_MS + 2000)
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


async def test_a_stream_without_heartbeats_becomes_unavailable() -> None:
    stack = CameraStack()
    data = liveness_jpeg((30, 40, 50))
    await stack.setup_camera(data)
    await stack.heartbeat()
    assert (await stack.look())["code"] == "ok"

    stack.clock[0] = CAMERA_LIVENESS_TIMEOUT_SECONDS + 1.0
    result = await stack.look()
    assert result["code"] == "camera_unavailable"


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
                ActiveSampler(stream_id="stream-budi-2", role="camera", page_ms=JOINED_MS + 6200)
            ]
        )
    )

    result = await stack.look()
    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
