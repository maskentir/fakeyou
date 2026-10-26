from typing import Any, cast

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from fakeyou.conversation.middleware import EvidenceRoutingMiddleware
from tests._conversation_fakes import ToolCallingFakeModel
from tests._persistence_fakes import RecordingJournal


@tool(description="Look at the shared screen.")
def look_at_screen(question: str) -> str:
    return f"The slide shows the beta date for: {question}"


@tool(description="Raise a hand in the meeting.")
def meet_raise_hand() -> str:
    return "queued"


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


def _look_call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "look_at_screen", "args": {"question": "date"}, "id": "look-1"}],
    )


async def test_a_reply_built_from_a_tool_result_goes_to_the_strong_model() -> None:
    light = ToolCallingFakeModel(responses=[_look_call()])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    agent = create_agent(
        model=light,
        tools=[look_at_screen],
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"look_at_screen"}))
        ],
    )

    result = await agent.ainvoke({"messages": [HumanMessage("Rani, what date is on the slide?")]})

    assert result["messages"][-1].text == "The beta is on 14 October."
    assert all(not isinstance(seen[-1], ToolMessage) for seen in light.seen)
    assert strong.seen
    assert all(isinstance(seen[-1], ToolMessage) for seen in strong.seen)


async def test_small_talk_stays_on_the_light_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Morning!")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"look_at_screen"}))
        ],
    )

    await agent.ainvoke({"messages": [HumanMessage("Morning, Rani")]})

    assert light.seen
    assert strong.seen == []


async def test_memory_found_for_this_turn_goes_straight_to_the_strong_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"recall_meeting_context"}))
        ],
    )
    messages: list[BaseMessage] = [
        *_prefetch_pair("t1", "found"),
        _trigger("Rani, when is the beta?", "t1"),
    ]

    result = await agent.ainvoke({"messages": cast(Any, messages)})

    assert result["messages"][-1].text == "The beta is on 14 October."
    assert light.seen == []
    assert strong.seen


async def test_memory_that_found_nothing_stays_on_the_light_model() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"recall_meeting_context"}))
        ],
    )
    messages: list[BaseMessage] = [
        *_prefetch_pair("t1", "empty"),
        _trigger("Morning, Rani", "t1"),
    ]

    await agent.ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_memory_found_for_an_earlier_turn_does_not_escalate() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"recall_meeting_context"}))
        ],
    )
    messages: list[BaseMessage] = [
        *_prefetch_pair("t0", "found"),
        _trigger("Morning, Rani", "t1"),
    ]

    await agent.ainvoke({"messages": cast(Any, messages)})

    assert light.seen
    assert strong.seen == []


async def test_parallel_tools_ending_in_a_meet_action_still_escalate() -> None:
    both = AIMessage(
        content="",
        tool_calls=[
            {"name": "look_at_screen", "args": {"question": "date"}, "id": "look-1"},
            {"name": "meet_raise_hand", "args": {}, "id": "hand-1"},
        ],
    )
    light = ToolCallingFakeModel(responses=[both])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    agent = create_agent(
        model=light,
        tools=[look_at_screen, meet_raise_hand],
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"look_at_screen"}))
        ],
    )

    result = await agent.ainvoke({"messages": [HumanMessage("Rani, what date is on the slide?")]})

    assert result["messages"][-1].text == "The beta is on 14 October."
    assert strong.seen


async def test_a_meet_action_alone_does_not_escalate() -> None:
    hand = AIMessage(
        content="",
        tool_calls=[{"name": "meet_raise_hand", "args": {}, "id": "hand-1"}],
    )
    light = ToolCallingFakeModel(responses=[hand, AIMessage(content="Done.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        tools=[meet_raise_hand],
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"look_at_screen"}))
        ],
    )

    await agent.ainvoke({"messages": [HumanMessage("Rani, raise your hand")]})

    assert light.seen
    assert strong.seen == []


async def test_the_synthetic_meeting_view_does_not_escalate() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Morning!")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"observe_meeting"}))
        ],
    )
    call = AIMessage(
        content="",
        tool_calls=[{"name": "observe_meeting", "args": {}, "id": "view-1", "type": "tool_call"}],
    )
    view = ToolMessage(
        content="Meeting view: ...",
        tool_call_id="view-1",
        name="observe_meeting",
        additional_kwargs={"synthetic_view": True},
    )

    await agent.ainvoke({"messages": [HumanMessage("Morning, Rani"), call, view]})

    assert light.seen
    assert strong.seen == []


async def test_the_chosen_route_is_journaled() -> None:
    journal = RecordingJournal()
    light = ToolCallingFakeModel(responses=[_look_call()])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="The beta is on 14 October.")])
    agent = create_agent(
        model=light,
        tools=[look_at_screen],
        middleware=[
            EvidenceRoutingMiddleware(
                strong, evidence_tools=frozenset({"look_at_screen"}), journal=journal
            )
        ],
    )

    await agent.ainvoke({"messages": [HumanMessage("Rani, what date is on the slide?")]})

    routes = journal.events("model_route_selected")
    assert [(row["route"], row["trigger"]) for row in routes] == [
        ("small", "none"),
        ("strong", "look_at_screen"),
    ]
