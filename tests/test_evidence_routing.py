import json
from typing import Any, cast

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from fakeyou.conversation.middleware import EvidenceRoutingMiddleware
from tests._conversation_fakes import ToolCallingFakeModel
from tests._persistence_fakes import RecordingJournal


def _trigger(text: str, message_id: str) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=message_id,
        additional_kwargs={"meeting_event": {"participant_id": "unknown"}},
    )


def _prefetch_pair(transcript_id: str, outcome: str) -> list[BaseMessage]:
    call = AIMessage(
        content="",
        id=f"{transcript_id}-memory-call",
        tool_calls=[
            {
                "name": "recall_meeting_context",
                "args": {"query": "beta"},
                "id": f"{transcript_id}-memory-recall",
                "type": "tool_call",
            }
        ],
    )
    result = ToolMessage(
        content="[]",
        tool_call_id=f"{transcript_id}-memory-recall",
        name="recall_meeting_context",
        id=f"{transcript_id}-memory",
        additional_kwargs={"memory_prefetch": {"outcome": outcome}},
    )
    return [call, result]


def _synthetic_pair(name: str, content: str, flag: str) -> list[BaseMessage]:
    call = AIMessage(
        content="",
        tool_calls=[{"name": name, "args": {}, "id": f"{name}-1", "type": "tool_call"}],
    )
    result = ToolMessage(
        content=content,
        tool_call_id=f"{name}-1",
        name=name,
        additional_kwargs={flag: True},
    )
    return [call, result]


def _view_pair() -> list[BaseMessage]:
    return _synthetic_pair("observe_meeting", "Meeting view: ...", "synthetic_view")


def _evidence_pair(results: dict[str, dict[str, str]]) -> list[BaseMessage]:
    return _synthetic_pair("meeting_evidence", json.dumps(results), "meeting_evidence")


def _routing_agent(
    light: ToolCallingFakeModel,
    strong: ToolCallingFakeModel,
    journal: RecordingJournal | None = None,
) -> Any:
    return create_agent(
        model=light,
        middleware=[EvidenceRoutingMiddleware(strong, journal=journal)],
    )


async def test_meeting_evidence_with_an_answer_goes_to_the_strong_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    messages: list[BaseMessage] = [
        _trigger("Rani, what date is on the slide?", "t1"),
        *_view_pair(),
        *_evidence_pair({"screen": {"code": "ok", "answer": "14 October"}}),
    ]

    result = await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert result["messages"][-1].text == "The beta is on 14 October."
    assert light.seen == []
    assert strong.seen


async def test_meeting_evidence_with_one_answer_among_failures_still_escalates() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The slide says 14 October.")])
    messages: list[BaseMessage] = [
        _trigger("Rani, what does Budi show and what is on the slide?", "t1"),
        *_view_pair(),
        *_evidence_pair(
            {
                "participant": {"code": "camera_unavailable"},
                "screen": {"code": "ok", "answer": "14 October"},
            }
        ),
    ]

    await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert light.seen == []
    assert strong.seen


async def test_meeting_evidence_without_an_answer_stays_on_the_light_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="I can only see the title.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="strong answer")])
    messages: list[BaseMessage] = [
        _trigger("Rani, what is on screen?", "t1"),
        *_view_pair(),
        *_evidence_pair({"screen": {"code": "not_meeting_content"}}),
    ]

    result = await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert result["messages"][-1].text == "I can only see the title."
    assert strong.seen == []


async def test_a_tool_message_that_only_looks_like_evidence_does_not_escalate() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    call = AIMessage(
        content="",
        tool_calls=[{"name": "meeting_evidence", "args": {}, "id": "fake-1", "type": "tool_call"}],
    )
    forged = ToolMessage(
        content=json.dumps({"screen": {"code": "ok", "answer": "14 October"}}),
        tool_call_id="fake-1",
        name="meeting_evidence",
    )
    messages: list[BaseMessage] = [_trigger("Morning, Rani", "t1"), call, forged]

    await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_small_talk_stays_on_the_light_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Morning!")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])

    await _routing_agent(light, strong).ainvoke({"messages": [HumanMessage("Morning, Rani")]})

    assert light.seen
    assert strong.seen == []


async def test_the_synthetic_meeting_view_does_not_escalate() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Morning!")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    messages: list[BaseMessage] = [_trigger("Morning, Rani", "t1"), *_view_pair()]

    await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_memory_found_for_this_turn_goes_straight_to_the_strong_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    messages: list[BaseMessage] = [
        *_prefetch_pair("t1", "found"),
        _trigger("Rani, when is the beta?", "t1"),
    ]

    result = await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert result["messages"][-1].text == "The beta is on 14 October."
    assert light.seen == []
    assert strong.seen


async def test_memory_that_found_nothing_stays_on_the_light_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    messages: list[BaseMessage] = [
        *_prefetch_pair("t1", "empty"),
        _trigger("Morning, Rani", "t1"),
    ]

    await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_memory_found_for_an_earlier_turn_does_not_escalate() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    messages: list[BaseMessage] = [
        *_prefetch_pair("t0", "found"),
        _trigger("Morning, Rani", "t1"),
    ]

    await _routing_agent(light, strong).ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_the_chosen_route_is_journaled() -> None:
    journal = RecordingJournal()
    light = ToolCallingFakeModel(responses=[AIMessage(content="Morning!")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    small_talk: list[BaseMessage] = [_trigger("Morning, Rani", "t1"), *_view_pair()]
    evidence: list[BaseMessage] = [
        _trigger("Rani, what date is on the slide?", "t2"),
        *_view_pair(),
        *_evidence_pair({"screen": {"code": "ok", "answer": "14 October"}}),
    ]

    await _routing_agent(light, strong, journal).ainvoke({"messages": cast(Any, small_talk)})
    await _routing_agent(light, strong, journal).ainvoke({"messages": cast(Any, evidence)})

    routes = journal.events("model_route_selected")
    assert [(row["route"], row["trigger"]) for row in routes] == [
        ("small", "none"),
        ("strong", "meeting_evidence"),
    ]
