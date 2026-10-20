import json

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.meet.meet_tools import MEET_ACTION_QUEUED, create_meet_action_tools
from fakeyou.memory.memory import create_memory_tools
from fakeyou.memory.persona import refuse_meet_tool
from fakeyou.perception.context import PerceptionContext, create_perception_tools
from fakeyou.perception.timeline import MeetingTimeline
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti

CONTRACT_TOOLS = {
    "recall_meeting_context",
    "observe_meeting",
    "look_at_screen",
    "look_at_participant",
    "meet_raise_hand",
    "meet_lower_hand",
    "meet_send_chat",
}


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def closed(self) -> bool:
        return False

    def request(self, kind: str, *, action_id: str, text: str = "") -> str:
        self.requests.append((kind, action_id, text))
        return MEET_ACTION_QUEUED


def _tool_calls(messages: list[object]) -> list[tuple[str, dict[str, object]]]:
    return [
        (str(call["name"]), dict(call["args"]))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]


def test_production_factories_expose_exactly_the_contract_tools() -> None:
    memory_tools = create_memory_tools(make_graphiti(), group_id="team_alpha")
    meet_tools = create_meet_action_tools(
        RecordingDispatcher(), refuse=refuse_meet_tool, journal=None
    )
    perception_tools = create_perception_tools(
        PerceptionContext(MeetingTimeline(), None, max_chars=6000)
    )

    exposed = {tool.name for tool in [*memory_tools, *meet_tools, *perception_tools]}

    assert exposed == CONTRACT_TOOLS


async def test_meeting_agent_raises_hand_through_the_production_tool() -> None:
    dispatcher = RecordingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=refuse_meet_tool, journal=None)
    model = ToolCallingFakeModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "meet_raise_hand", "args": {}, "id": "call-1", "type": "tool_call"}
                ],
            ),
            AIMessage(content="I raised my hand."),
        ]
    )
    graph = create_meeting_agent(model, tools=tools, checkpointer=InMemorySaver())

    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "Raise your hand, you have a question"}]},
        {"configurable": {"thread_id": "eval-thread"}},
    )

    assert _tool_calls(result["messages"]) == [("meet_raise_hand", {})]
    assert dispatcher.requests == [("raise_hand", "call-1", "")]
    tool_messages = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert isinstance(tool_messages[0].content, str)
    assert json.loads(tool_messages[0].content) == {"code": "queued", "message": MEET_ACTION_QUEUED}
    assert result["messages"][-1].content == "I raised my hand."


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
