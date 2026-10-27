from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.meet.meet_tools import create_action_status_tool
from fakeyou.memory.memory import create_memory_tools
from fakeyou.perception.context import PerceptionContext, create_perception_tools
from fakeyou.perception.timeline import MeetingTimeline
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti

CONTRACT_TOOLS = {
    "recall_meeting_context",
    "look_at_screen",
    "look_at_participant",
    "meet_check_action",
    "look_back_chat",
}


def _tool_calls(messages: list[object]) -> list[tuple[str, dict[str, object]]]:
    return [
        (str(call["name"]), dict(call["args"]))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]


def test_production_factories_expose_exactly_the_contract_tools() -> None:
    memory_tools = create_memory_tools(make_graphiti(), group_id="team_alpha")
    status_tool = create_action_status_tool(tuple)
    perception_tools = create_perception_tools(
        PerceptionContext(MeetingTimeline(), None, max_chars=6000)
    )

    exposed = {tool.name for tool in [*memory_tools, status_tool, *perception_tools]}

    assert exposed == CONTRACT_TOOLS


async def test_meeting_agent_recalls_through_the_production_tool() -> None:
    memory = make_graphiti(facts=["The budget review moved to Friday."])
    tools = [
        tool
        for tool in create_memory_tools(memory, group_id="team_alpha")
        if tool.name == "recall_meeting_context"
    ]
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "recall_meeting_context",
                        "args": {"query": "budget review day"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="It moved to Friday."),
        ]
    )
    graph = create_meeting_agent(model, tools=tools, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "When is the budget review?"}]},
        {"configurable": {"thread_id": "eval-recall"}},
    )

    assert _tool_calls(result["messages"]) == [
        ("recall_meeting_context", {"query": "budget review day"})
    ]
    assert memory.searched[0]["query"] == "budget review day"
