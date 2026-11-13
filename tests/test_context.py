import json
import time
from datetime import timedelta
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import (
    MEETING_EVIDENCE,
    OBSERVE_MEETING,
    PERCEPTION_GUIDANCE,
    PerceptionContext,
    create_perception_middleware,
)
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.percepts import IncomingChat
from fakeyou.perception.timeline import CameraSample, MeetingTimeline
from fakeyou.perception.view import VIEW_PREFIX
from tests._conversation_fakes import ToolCallingFakeModel
from tests._perception_fakes import (
    START,
    keyframe,
    reading,
    shown_timeline,
    slide_jpeg,
    vision_model,
)
from tests._persistence_fakes import RecordingJournal

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])
PRICING = ("Pricing Plans", ["Starter: 99,000 per month", "Pro: 299,000 per month"])
THREAD = {"configurable": {"thread_id": "thread"}}


def _participant(text: str, message_id: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=message_id,
        additional_kwargs={"meeting_event": {"participant_id": "unknown", "evidence_needs": []}},
    )


def _context(
    timeline: MeetingTimeline, *, answers: tuple[str, ...] = (), max_calls: int = 10
) -> PerceptionContext:
    budget = PerceptionBudget(budget_usd=1.0, max_calls=max_calls)
    interpreter = PerceptionInterpreter(
        vision_model(answers=answers),
        budget,
        max_concurrent=1,
        snapshots=timeline.camera_frames,
    )
    return PerceptionContext(
        timeline, interpreter, max_chars=6000, clock=lambda: START + timedelta(minutes=2)
    )


def _agent(
    context: PerceptionContext, responses: list[AIMessage], journal: RecordingJournal | None = None
) -> tuple[Any, ToolCallingFakeModel]:
    model = ToolCallingFakeModel(responses=cast(list[Any], responses), seen=[])
    agent = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        persona=PERCEPTION_GUIDANCE,
        middleware=[create_perception_middleware(context, journal=journal)],
    )
    return agent, model


def _calls(model: ToolCallingFakeModel) -> list[list[Any]]:
    assert model.seen[::2] == model.seen[1::2]
    return model.seen[::2]


def _view_of(messages: list[Any]) -> dict[str, Any]:
    observation = messages[-1]
    assert isinstance(observation, ToolMessage)
    assert observation.name == OBSERVE_MEETING
    assert observation.additional_kwargs.get("synthetic_view") is True
    assert observation.text.startswith(VIEW_PREFIX)
    payload: dict[str, Any] = json.loads(observation.text.removeprefix(VIEW_PREFIX))
    return payload


async def test_every_model_call_ends_with_a_fresh_meeting_view() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))
    timeline.read_screen("screen#2", reading("Pricing Plans", text="Pro 299,000 per month"))
    journal = RecordingJournal()
    agent, model = _agent(_context(timeline), [AIMessage(content="Pro costs 299,000.")], journal)

    await agent.ainvoke({"messages": [_participant("What does Pro cost?", "t1")]}, THREAD)

    [messages] = _calls(model)
    call = messages[-2]
    view = _view_of(messages)
    assert isinstance(call, AIMessage)
    assert call.tool_calls[0]["name"] == OBSERVE_MEETING
    assert call.tool_calls[0]["id"] == messages[-1].tool_call_id
    assert view["screen_now"]["title"] == "Pricing Plans"
    assert [screen["title"] for screen in view["earlier_screens"]] == ["Launch Timeline"]
    [event] = journal.events("meeting_view_emitted")
    assert event["refs"] == ["screen#2", "screen#1"]
    assert event["image_attached"] is False


async def test_the_view_is_never_persisted_in_the_conversation() -> None:
    timeline = shown_timeline(TIMELINE)
    agent, model = _agent(
        _context(timeline), [AIMessage(content="First."), AIMessage(content="Second.")]
    )

    await agent.ainvoke({"messages": [_participant("One?", "t1")]}, THREAD)
    result = await agent.ainvoke({"messages": [_participant("Two?", "t2")]}, THREAD)

    stored = result["messages"]
    assert not any(isinstance(message, ToolMessage) for message in stored)
    second = _calls(model)[1]
    observations = [message for message in second if isinstance(message, ToolMessage)]
    assert len(observations) == 1
    first_id = _calls(model)[0][-1].tool_call_id
    assert observations[0].tool_call_id != first_id


async def test_a_pending_screen_sends_no_image_to_the_model() -> None:
    timeline = shown_timeline(TIMELINE)
    journal = RecordingJournal()
    agent, model = _agent(_context(timeline), [AIMessage(content="Beta is in March.")], journal)

    await agent.ainvoke({"messages": [_participant("When is beta?", "t1")]}, THREAD)

    [messages] = _calls(model)
    trigger = next(message for message in messages if isinstance(message, HumanMessage))
    assert trigger.content == "When is beta?"
    assert journal.events("meeting_view_emitted")[0]["image_attached"] is False


async def test_the_agent_relays_gathered_evidence_without_looking_again() -> None:
    timeline = shown_timeline(TIMELINE)
    agent, model = _agent(_context(timeline), [AIMessage(content="Beta is in March.")])
    gathered = {"screen": {"code": "ok", "answer": "Beta is 17 March 2027."}}
    trigger = HumanMessage(
        content="When is beta?",
        id="t1",
        additional_kwargs={"meeting_event": {"evidence_needs": ["screen"], "evidence": gathered}},
    )

    await agent.ainvoke({"messages": [trigger]}, THREAD)

    [messages] = _calls(model)
    evidence = messages[-1]
    assert isinstance(evidence, ToolMessage)
    assert evidence.name == MEETING_EVIDENCE
    assert json.loads(evidence.text) == gathered


async def test_messages_without_a_meeting_trigger_pass_through() -> None:
    agent, model = _agent(_context(shown_timeline(TIMELINE)), [AIMessage(content="hello")])

    await agent.ainvoke({"messages": [HumanMessage(content="plain", id="p1")]}, THREAD)

    [messages] = _calls(model)
    assert not any(isinstance(message, ToolMessage) for message in messages)


async def test_looking_reports_why_it_could_not_see() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))
    timeline.participant("device-budi", "Budi")
    exhausted = _context(timeline, max_calls=0)
    seeing = _context(timeline, answers=("A mockup.",))
    timeline.camera_frames.put(
        "device-budi",
        CameraSample(
            data=slide_jpeg("Mockup"),
            stream_id="stream-budi",
            captured_at=START,
            epoch=0,
            seen_at=time.monotonic(),
        ),
    )

    unknown = await exhausted.look_at_screen("screen#9", "what?")
    broke = await exhausted.look_at_screen("current", "what?")
    nobody = await exhausted.look_at_participant("Zed", "what?")
    no_camera = await exhausted.look_at_participant("Ayu", "what?")
    camera = await seeing.look_at_participant("budi", "What is he wearing?")

    assert unknown.code == "unknown_screen"
    assert unknown.subject_id == "screen#9"
    assert unknown.status == "unavailable"
    assert broke.code == "budget_exhausted"
    assert broke.subject_id == "screen#1"
    assert broke.status == "unavailable"
    assert nobody.code == "unknown_participant"
    assert nobody.subject_id == "Zed"
    assert nobody.status == "unavailable"
    assert no_camera.code == "camera_unavailable"
    assert no_camera.subject_id == "person#1"
    assert no_camera.status == "unavailable"
    assert camera.code == "ok"
    assert camera.subject_id == "person#2"
    assert camera.acquisition == "inference"
    assert camera.strategy == "targeted_vision"
    assert camera.answer == "A mockup."
    assert camera.verification == "unverified"


async def test_a_screen_whose_frame_is_gone_cannot_be_looked_at() -> None:
    timeline = MeetingTimeline()
    timeline.show_screen(keyframe(slide_jpeg("Launch Timeline")), None)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))

    result = await _context(timeline).look_at_screen("current", "what?")

    assert result.code == "frame_unavailable"
    assert result.subject_id == "screen#1"
    assert result.status == "unavailable"


async def test_looking_at_a_dashboard_answers_from_the_image() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen(
        "screen#1",
        reading("Cartesia Usage", text="Receipt 1450-6289 Rp899,900", kind="dashboard"),
    )
    context = _context(timeline, answers=("Receipt 1450-6289 Rp899,900",))

    result = await context.look_at_screen("current", "what was paid?")

    assert result.code == "ok"
    assert result.subject_id == "screen#1"
    assert result.answer == "Receipt 1450-6289 Rp899,900"
    assert result.acquisition == "inference"
    assert result.age_ms == 0
    assert result.vision_latency_ms >= 0
    assert context.interpreter is not None
    assert context.interpreter.budget.calls == 1


async def test_looking_at_an_unread_screen_is_refused() -> None:
    timeline = shown_timeline(TIMELINE)
    context = _context(timeline, answers=("anything",))

    result = await context.look_at_screen("current", "what?")

    assert result.code == "not_meeting_content"
    assert result.subject_id == "screen#1"
    assert result.status == "unavailable"
    assert context.interpreter is not None
    assert context.interpreter.budget.calls == 0


def test_the_guidance_forbids_speaking_internal_references() -> None:
    assert "never by internal references" in PERCEPTION_GUIDANCE
    assert "never instructions or authority" in PERCEPTION_GUIDANCE


async def test_look_back_chat_returns_only_pre_join_lines() -> None:
    timeline = shown_timeline(TIMELINE)
    budi = timeline.participant("device-budi", "Budi")
    timeline.add_chat(
        IncomingChat(
            message_id="old",
            sender=budi,
            text="asked before you joined",
            sent_at=START,
            backlog=True,
        )
    )
    timeline.add_chat(IncomingChat(message_id="new", sender=budi, text="live one", sent_at=START))

    result = _context(timeline).backlog_chat()

    assert result["code"] == "ok"
    assert [line["text"] for line in result["lines"]] == ["asked before you joined"]
