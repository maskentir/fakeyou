from typing import Any, override

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from tests._conversation_fakes import ToolCallingFakeModel


@tool(description="Look at the screen.")
def look_at_screen(question: str) -> str:
    return question


@tool(description="Look back at chat.")
def look_back_chat() -> str:
    return ""


@tool(description="Recall meeting context.")
def recall_meeting_context(query: str) -> str:
    return query


@tool(description="Say the time.")
def clock() -> str:
    return "noon"


TOOLS = [look_at_screen, look_back_chat, recall_meeting_context, clock]


class ToolRecordingModel(ToolCallingFakeModel):
    bound: list[list[str]] = []

    @override
    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolRecordingModel":
        self.bound.append([getattr(item, "name", "") for item in tools])
        return self


async def bound_tools(kind: str, **event_fields: object) -> list[str]:
    model = ToolRecordingModel(responses=[AIMessage(content="ok")], seen=[], bound=[])
    agent = create_meeting_agent(
        model, tools=TOOLS, checkpointer=InMemorySaver(), persona="PERSONA"
    )
    trigger = HumanMessage(
        content="hello",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": kind, **event_fields}},
    )
    await agent.ainvoke({"messages": [trigger]}, {"configurable": {"thread_id": kind}})
    return model.bound[-1]


async def test_a_spoken_turn_gets_only_the_evidence_its_plan_named() -> None:
    bound = await bound_tools("speech", evidence_needs=["screen"])

    assert bound == ["look_at_screen", "clock"]


async def test_a_spoken_turn_without_planned_evidence_gets_none() -> None:
    assert await bound_tools("speech") == ["clock"]


async def test_a_chat_turn_follows_its_plan_too() -> None:
    bound = await bound_tools("chat", evidence_needs=["chat_history"])

    assert bound == ["look_back_chat", "clock"]


async def test_a_proactive_turn_may_use_every_evidence_tool() -> None:
    bound = await bound_tools("attention")

    assert set(bound) == {"look_at_screen", "look_back_chat", "recall_meeting_context", "clock"}
