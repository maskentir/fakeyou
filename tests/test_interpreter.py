import base64
import io

import pytest
from langchain_core.messages import HumanMessage
from PIL import Image

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.perception.budget import PerceptionBudget, usd_for
from fakeyou.perception.interpreter import (
    PAYLOAD_MAX_EDGE,
    PREVIOUS_SCREEN_PROMPT,
    PerceptionInterpreter,
    ScreenReader,
)
from fakeyou.perception.timeline import MeetingTimeline
from tests._perception_fakes import (
    FailingVisionModel,
    FakeVisionModel,
    content_block,
    keyframe,
    reading,
    slide_jpeg,
    vision_model,
)
from tests._persistence_fakes import RecordingJournal

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])
PRICING = ("Pricing Plans", ["Starter: 99,000 per month", "Pro: 299,000 per month"])


def _interpreter(
    model: FakeVisionModel, *, budget_usd: float = 1.0, max_calls: int = 10
) -> PerceptionInterpreter:
    budget = PerceptionBudget(budget_usd=budget_usd, max_calls=max_calls)
    return PerceptionInterpreter(model, budget, max_concurrent=2)


def _sent_image(message: HumanMessage) -> Image.Image:
    block = content_block(message, 0)
    assert block["type"] == "image"
    assert block["mime_type"] == "image/jpeg"
    return Image.open(io.BytesIO(base64.b64decode(block["base64"])))


async def test_a_screen_is_read_into_structured_knowledge() -> None:
    expected = reading("Launch Timeline", figures=[("Beta", "17 March 2027")])
    model = vision_model([expected], reading_tokens=1800)
    interpreter = _interpreter(model)

    result = await interpreter.read_screen(slide_jpeg(*TIMELINE, size=(1920, 1080)))

    [[message]] = model.structured_seen
    assert isinstance(message, HumanMessage)
    assert result == expected
    assert max(_sent_image(message).size) == PAYLOAD_MAX_EDGE
    assert "verbatim" in content_block(message, 1)["text"]
    assert interpreter.budget.spent_usd == usd_for(1800)


async def test_the_previous_title_gives_the_reader_continuity() -> None:
    model = vision_model([reading("Pricing Plans")])

    await _interpreter(model).read_screen(slide_jpeg(*PRICING), previous_title="Launch Timeline")

    [[message]] = model.structured_seen
    prompt = content_block(message, 1)["text"]
    assert PREVIOUS_SCREEN_PROMPT.format(title="Launch Timeline") in prompt


async def test_an_exhausted_budget_reads_nothing() -> None:
    model = vision_model([reading("Launch Timeline")])

    result = await _interpreter(model, max_calls=0).read_screen(slide_jpeg(*TIMELINE))

    assert result is None
    assert model.structured_seen == []


async def test_an_unparsed_reading_is_a_named_error_but_still_costs_tokens() -> None:
    model = vision_model([None])
    interpreter = _interpreter(model)

    with pytest.raises(FakeYouError, match="no screen reading") as caught:
        await interpreter.read_screen(slide_jpeg(*TIMELINE))

    assert caught.value.code is ErrorCode.PERCEPTION_READING_UNPARSED
    assert interpreter.budget.calls == 1
    assert interpreter.budget.spent_usd > 0


async def test_provider_errors_are_classified_and_refund_the_reservation() -> None:
    model = FailingVisionModel(responses=[], seen=[], structured_seen=[])
    interpreter = _interpreter(model)

    with pytest.raises(ProviderFault):
        await interpreter.read_screen(slide_jpeg(*TIMELINE))

    assert interpreter.budget.calls == 0
    assert interpreter.budget.spent_usd == 0


async def test_answer_looks_at_the_image_for_a_specific_question() -> None:
    model = vision_model(answers=["The beta is on 17 March 2027."])
    interpreter = _interpreter(model)

    answer = await interpreter.answer(slide_jpeg(*TIMELINE), "When is the beta?")

    [[message]] = model.seen
    assert answer == "The beta is on 17 March 2027."
    assert "Question: When is the beta?" in content_block(message, 1)["text"]
    assert interpreter.budget.calls == 1


async def test_the_reader_reads_each_new_screen_once() -> None:
    timeline = MeetingTimeline()
    model = vision_model([reading("Launch Timeline"), reading("Pricing Plans")])
    reader = ScreenReader(timeline, _interpreter(model))
    first, second = slide_jpeg(*TIMELINE), slide_jpeg(*PRICING)

    timeline.show_screen(keyframe(first), None, data=first)
    await reader.settle()
    timeline.show_screen(keyframe(second, seconds=30), None, data=second)
    await reader.settle()
    timeline.show_screen(keyframe(first, seconds=60), None, data=first)
    await reader.settle()

    titles = [
        screen.reading.title for screen in timeline.snapshot().screens if screen.reading is not None
    ]
    assert titles == ["Launch Timeline", "Pricing Plans"]
    assert len(model.structured_seen) == 2
    prompt = content_block(model.structured_seen[1][0], 1)["text"]
    assert PREVIOUS_SCREEN_PROMPT.format(title="Launch Timeline") in prompt
    await reader.close()


async def test_a_failed_read_marks_the_screen_and_is_journaled() -> None:
    journal = RecordingJournal()
    timeline = MeetingTimeline()
    model = FailingVisionModel(responses=[], seen=[], structured_seen=[])
    reader = ScreenReader(timeline, _interpreter(model), journal=journal)
    data = slide_jpeg(*TIMELINE)

    timeline.show_screen(keyframe(data), None, data=data)
    await reader.settle()

    [screen] = timeline.snapshot().screens
    [event] = journal.events("screen_reading_failed")
    assert screen.reading_failed
    assert event["error_type"] == "ProviderFault"


async def test_a_screen_whose_frame_was_evicted_is_skipped() -> None:
    journal = RecordingJournal()
    timeline = MeetingTimeline()
    model = vision_model([reading("Launch Timeline")])
    reader = ScreenReader(timeline, _interpreter(model), journal=journal)

    timeline.show_screen(keyframe(slide_jpeg(*TIMELINE)), None)
    await reader.settle()

    [screen] = timeline.snapshot().screens
    assert screen.reading_failed
    assert journal.events("screen_reading_skipped")[0]["reason"] == "frame_evicted"
    assert model.structured_seen == []


async def test_finished_reads_are_journaled_with_their_kind() -> None:
    journal = RecordingJournal()
    timeline = MeetingTimeline()
    reader = ScreenReader(
        timeline, _interpreter(vision_model([reading("Meet", kind="meeting_ui")])), journal=journal
    )
    data = slide_jpeg(*TIMELINE)

    timeline.show_screen(keyframe(data), None, data=data)
    await reader.settle()

    [event] = journal.events("screen_reading_finished")
    assert (event["ref"], event["content_kind"], event["budget_exhausted"]) == (
        "screen#1",
        "meeting_ui",
        False,
    )
