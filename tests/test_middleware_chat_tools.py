from typing import Any, override

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.middleware import ChatTurnToolsMiddleware
from fakeyou.meet.meet_tools import MEET_SEND_CHAT
from tests._conversation_fakes import ToolCallingFakeModel


@tool(MEET_SEND_CHAT, description="Send a chat message.")
def fake_send_chat(message: str) -> str:
    return message


@tool(description="Look at something.")
def look(question: str) -> str:
    return question


class ToolRecordingModel(ToolCallingFakeModel):
    bound: list[list[str]] = []

    @override
    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolRecordingModel":
        self.bound.append([getattr(item, "name", "") for item in tools])
        return self


async def _bound_tools(kind: str) -> list[str]:
    model = ToolRecordingModel(responses=[AIMessage(content="ok")], seen=[], bound=[])
    agent = create_meeting_agent(
        model,
        tools=[fake_send_chat, look],
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[ChatTurnToolsMiddleware()],
    )
    trigger = HumanMessage(
        content="hello", id="m1", additional_kwargs={"meeting_event": {"kind": kind}}
    )
    await agent.ainvoke({"messages": [trigger]}, {"configurable": {"thread_id": kind}})
    return model.bound[-1]


async def test_chat_turns_cannot_post_a_second_chat_message() -> None:
    assert await _bound_tools("chat") == ["look"]


async def test_speech_turns_keep_the_chat_tool() -> None:
    assert sorted(await _bound_tools("speech")) == ["look", MEET_SEND_CHAT]
