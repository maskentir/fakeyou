from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from fakeyou.conversation.middleware import EvidenceRoutingMiddleware
from tests._conversation_fakes import ToolCallingFakeModel


@tool
def look_at_screen(question: str) -> str:
    """Look at the shared screen."""
    return f"The slide shows the beta date for: {question}"


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


async def test_prefetched_memory_does_not_count_as_the_twin_looking_something_up() -> None:
    light = ToolCallingFakeModel(responses=[AIMessage(content="Hi.")])
    strong = ToolCallingFakeModel(responses=[AIMessage(content="unused")])
    agent = create_agent(
        model=light,
        middleware=[
            EvidenceRoutingMiddleware(strong, evidence_tools=frozenset({"recall_meeting_context"}))
        ],
    )
    prefetch_call = AIMessage(
        content="",
        tool_calls=[{"name": "recall_meeting_context", "args": {"query": "hi"}, "id": "m-1"}],
    )
    prefetch_result = ToolMessage(
        content="[]",
        tool_call_id="m-1",
        name="recall_meeting_context",
        additional_kwargs={"memory_prefetch": {"outcome": "ok"}},
    )

    await agent.ainvoke({"messages": [HumanMessage("Hi"), prefetch_call, prefetch_result]})

    assert light.seen
    assert strong.seen == []
