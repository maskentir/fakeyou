from langchain_core.messages import AIMessage, HumanMessage

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.meet.meet_action_ledger import MeetActionKind
from fakeyou.meet.meet_tools import create_meet_action_tools
from tests._conversation_fakes import ToolCallingFakeModel


class _CountingDispatcher:
    enabled = True
    closed = False

    def __init__(self) -> None:
        self.requests: list[tuple[MeetActionKind, str, str]] = []

    def request(self, kind: MeetActionKind, *, action_id: str, text: str = "") -> str:
        self.requests.append((kind, action_id, text))
        return action_id


def _send(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "meet_send_chat",
                "args": {"message": "Hello from chat"},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


async def test_a_second_chat_send_in_the_same_turn_never_reaches_the_dispatcher() -> None:
    dispatcher = _CountingDispatcher()
    tools = create_meet_action_tools(dispatcher, refuse=lambda _name, _journal=None: "refused")
    model = ToolCallingFakeModel(
        responses=[_send("c1"), _send("c2"), AIMessage(content="I sent it in chat.")]
    )
    agent = create_meeting_agent(model, tools=tools)

    result = await agent.ainvoke({"messages": [HumanMessage("Harun, send a hello in chat please")]})

    assert dispatcher.requests == [("send_chat", "c1", "Hello from chat")]
    assert result["messages"][-1].text
