import asyncio
import json
import time
from datetime import UTC, datetime

from langchain_core.messages import AIMessage

from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.camera_evidence import CameraObservation
from fakeyou.perception.context import MeetingEvidence, PerceptionContext
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.timeline import CameraFrame, MeetingTimeline
from tests._perception_fakes import FakeVisionModel, vision_model
from tests._persistence_fakes import RecordingJournal


def observation(participant_id: str = "p1") -> CameraObservation:
    return CameraObservation(
        participant_id=participant_id,
        observed_at=datetime.now(UTC),
        clothing="light ivory shirt",
        visible_objects=["microphone"],
        background="indoor room",
        appearance="a person speaking",
    )


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
    import io

    from PIL import Image

    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def test_the_same_frame_is_described_once() -> None:
    model = vision_model()
    model.observations = [observation()]
    interpreter = interpreter_with(model)
    frame = jpeg_of((200, 180, 160))

    first = await interpreter.camera_observation("p1", frame)
    second = await interpreter.camera_observation("p1", frame)

    assert first is not None
    assert first.resolution == "fresh"
    assert second is not None
    assert second.resolution == "cache_hit"
    assert second.vision_latency_ms == 0
    assert len(model.observations) == 0


async def test_a_different_frame_is_described_again() -> None:
    model = vision_model()
    model.observations = [observation(), observation()]
    interpreter = interpreter_with(model)

    await interpreter.camera_observation("p1", jpeg_of((10, 10, 10)))
    second = await interpreter.camera_observation("p1", jpeg_of((250, 250, 250)))

    assert second is not None
    assert second.resolution == "fresh"
    assert len(model.observations) == 0


async def test_another_participant_is_described_separately() -> None:
    model = vision_model()
    model.observations = [observation("p1"), observation("p2")]
    interpreter = interpreter_with(model)
    frame = jpeg_of((120, 120, 120))

    await interpreter.camera_observation("p1", frame)
    other = await interpreter.camera_observation("p2", frame)

    assert other is not None
    assert other.resolution == "fresh"
    assert len(model.observations) == 0


async def test_two_questions_about_one_frame_share_one_vision_call() -> None:
    model = vision_model()
    model.observations = [observation()]
    interpreter = interpreter_with(model)
    frame = jpeg_of((90, 90, 90))

    first, second = await asyncio.gather(
        interpreter.camera_observation("p1", frame),
        interpreter.camera_observation("p1", frame),
    )

    assert first is not None
    assert second is not None
    assert {first.resolution, second.resolution} == {"cache_hit", "fresh"}
    assert len(model.observations) == 0


async def test_a_stale_description_is_not_reused() -> None:
    clock = [0.0]
    model = vision_model()
    model.observations = [observation(), observation()]
    interpreter = interpreter_with(model)
    object.__setattr__(interpreter, "_monotonic", lambda: clock[0])
    frame = jpeg_of((60, 60, 60))

    await interpreter.camera_observation("p1", frame)
    clock[0] = 3600.0
    later = await interpreter.camera_observation("p1", frame)

    assert later is not None
    assert later.resolution == "fresh"


async def test_resolving_evidence_is_journalled_with_its_resolution() -> None:
    from fakeyou.perception.context import MeetingEvidence

    model = vision_model()
    model.observations = [observation()]
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


def cam_observation(participant_id: str = "device-p1") -> CameraObservation:
    return CameraObservation(
        participant_id=participant_id,
        observed_at=datetime.now(UTC),
        clothing="light ivory shirt",
        visible_objects=[],
        background="indoor room",
        appearance="a person speaking",
    )


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


async def test_schema_question_uses_cached_observation_without_vision_call() -> None:
    model = vision_model()
    model.observations = [cam_observation()]
    context = live_context(model)

    first = json.loads(await context.look_at_participant("Budi", "What color is his shirt?"))
    calls = len(model.seen) + len(model.structured_seen)
    second = json.loads(await context.look_at_participant("Budi", "What is he wearing?"))

    assert first["code"] == "ok"
    assert first["resolution"] == "fresh"
    assert first["sufficiency"] == "sufficient"
    assert first["strategy"] == "structured_observation"
    assert second["resolution"] == "cache_hit"
    assert second["sufficiency"] == "sufficient"
    assert len(model.seen) + len(model.structured_seen) == calls


async def test_detail_question_with_cached_observation_does_targeted_vision() -> None:
    model = vision_model()
    model.observations = [cam_observation()]
    model.responses = [AIMessage(content="The shirt says HELLO.")]
    context = live_context(model)

    await context.look_at_participant("Budi", "What is he wearing?")
    result = json.loads(await context.look_at_participant("Budi", "What text is on his shirt?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "targeted_vision"
    assert result["sufficiency"] == "sufficient"
    assert "HELLO" in result["answer"]
    assert len(model.observations) == 0
    assert len(model.seen) == 1


async def test_detail_question_without_observation_goes_directly_to_targeted_vision() -> None:
    model = vision_model()
    model.responses = [AIMessage(content="Three fingers.")]
    context = live_context(model)

    result = json.loads(
        await context.look_at_participant("Budi", "How many fingers is he showing?")
    )

    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "targeted_vision"
    assert result["vision_latency_ms"] >= 0
    assert len(model.observations) == 0
    assert len(model.seen) == 1


async def test_schema_question_without_observation_uses_generic_observation() -> None:
    model = vision_model()
    model.observations = [cam_observation()]
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What is he wearing?"))

    assert result["code"] == "ok"
    assert result["resolution"] == "fresh"
    assert result["strategy"] == "structured_observation"
    assert result["sufficiency"] == "sufficient"
    assert len(model.observations) == 0


async def test_detail_not_visible_returns_insufficient_without_guessing() -> None:
    model = vision_model()
    model.responses = [AIMessage(content="The text is not visible in the frame.")]
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What text is on his shirt?"))

    assert result["code"] == "insufficient_evidence"
    assert result["resolution"] == "fresh"
    assert result["sufficiency"] == "insufficient"
    assert result["strategy"] == "targeted_vision"
    assert "answer" not in result


async def test_change_question_is_insufficient_without_vision_call() -> None:
    model = vision_model()
    context = live_context(model)

    result = json.loads(await context.look_at_participant("Budi", "What changed since earlier?"))

    assert result["code"] == "insufficient_evidence"
    assert result["sufficiency"] == "insufficient"
    assert result["strategy"] == "structured_observation"
    assert result["vision_latency_ms"] == 0
    assert len(model.seen) + len(model.structured_seen) == 0


async def test_budget_exhausted_for_camera_does_not_guess() -> None:
    model = vision_model()
    context = live_context(model, max_calls=0)

    result = json.loads(await context.look_at_participant("Budi", "What is he wearing?"))

    assert result["code"] == "budget_exhausted"
    assert result["strategy"] == "structured_observation"


async def test_participant_resolution_journals_strategy_and_sufficiency() -> None:
    model = vision_model()
    model.observations = [cam_observation()]
    journal = RecordingJournal()
    context = live_context(model, journal=journal)
    evidence = MeetingEvidence(context, journal=journal)

    await evidence("What is he wearing?", ("participant",), "t1")

    [resolved] = journal.events("perception_evidence_resolved")
    assert resolved["strategy"] == "structured_observation"
    assert resolved["sufficiency"] == "sufficient"
