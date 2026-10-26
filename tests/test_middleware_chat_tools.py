from typing import Any, override

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.middleware import ChannelToolsMiddleware, _pending_stamped_action
from fakeyou.meet.meet_tools import MEET_SEND_CHAT
from tests._conversation_fakes import ToolCallingFakeModel


@tool(MEET_SEND_CHAT, description="Send a chat message.")
def fake_send_chat(message: str) -> str:
    return message


@tool(description="Look at something.")
def look(question: str) -> str:
    return question


@tool(description="Mute yourself.")
def meet_mute() -> str:
    return "queued"


@tool(description="Raise a hand.")
def meet_raise_hand() -> str:
    return "queued"


@tool(description="Check an action.")
def meet_check_action(action_id: str) -> str:
    return action_id


@tool(description="Look at the screen.")
def look_at_screen(question: str) -> str:
    return question


@tool(description="Look at a participant.")
def look_at_participant(participant: str) -> str:
    return participant


@tool(description="Look back at chat.")
def look_back_chat() -> str:
    return ""


@tool(description="Recall meeting context.")
def recall_meeting_context(query: str) -> str:
    return query


class ToolRecordingModel(ToolCallingFakeModel):
    bound: list[list[str]] = []

    @override
    def bind_tools(self, tools: Any, **kwargs: Any) -> "ToolRecordingModel":
        self.bound.append([getattr(item, "name", "") for item in tools])
        return self


async def _bound_tools(
    kind: str, tools: list[Any] | None = None, **event_fields: object
) -> list[str]:
    model = ToolRecordingModel(responses=[AIMessage(content="ok")], seen=[], bound=[])
    agent = create_meeting_agent(
        model,
        tools=tools or [fake_send_chat, look, meet_mute, meet_raise_hand, meet_check_action],
        checkpointer=InMemorySaver(),
        persona="PERSONA",
        middleware=[ChannelToolsMiddleware()],
    )
    trigger = HumanMessage(
        content="hello",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": kind, **event_fields}},
    )
    await agent.ainvoke({"messages": [trigger]}, {"configurable": {"thread_id": kind}})
    return model.bound[-1]


async def test_chat_turns_cannot_post_a_second_chat_message() -> None:
    bound = await _bound_tools("chat")
    assert MEET_SEND_CHAT not in bound
    assert "meet_mute" in bound
    assert "meet_raise_hand" in bound


async def test_speech_turns_without_a_chat_request_cannot_send_chat() -> None:
    assert await _bound_tools("speech", turn_planned=True) == ["look"]


async def test_speech_turns_without_a_meet_action_stamp_bind_no_meet_actions() -> None:
    bound = await _bound_tools("speech")
    assert "meet_mute" not in bound
    assert "meet_raise_hand" not in bound


async def test_a_meet_action_stamp_binds_only_that_action() -> None:
    bound = await _bound_tools("speech", meet_action="mute")
    assert "meet_mute" in bound
    assert "meet_raise_hand" not in bound
    assert MEET_SEND_CHAT not in bound


async def test_speech_turns_that_request_chat_keep_the_chat_tool() -> None:
    bound = await _bound_tools("speech", requests_chat=True)
    assert sorted(bound) == ["look", MEET_SEND_CHAT]


async def test_attention_turns_cannot_send_chat() -> None:
    bound = await _bound_tools("attention", requests_chat=True, meet_action="mute")
    assert bound == ["look"]


async def test_speech_turns_bind_only_the_judged_evidence() -> None:
    planned = [
        look_at_screen,
        look_at_participant,
        look_back_chat,
        recall_meeting_context,
        meet_check_action,
    ]

    assert await _bound_tools("speech", planned, turn_planned=True) == []
    assert await _bound_tools(
        "speech", planned, turn_planned=True, evidence_needs=["screen", "memory"]
    ) == [
        "look_at_screen",
        "recall_meeting_context",
    ]
    assert await _bound_tools(
        "speech", planned, turn_planned=True, evidence_needs=["action_status"]
    ) == ["meet_check_action"]
    assert (
        await _bound_tools(
            "speech",
            planned,
            turn_planned=True,
            meet_action="react",
            evidence_needs=["screen"],
        )
        == []
    )
    planned_chat = [meet_mute, meet_raise_hand, look_at_screen]
    assert await _bound_tools(
        "chat",
        planned_chat,
        turn_planned=True,
        meet_action="mute",
        evidence_needs=["screen"],
    ) == ["meet_mute"]
    assert await _bound_tools(
        "chat", planned_chat, turn_planned=True, evidence_needs=["screen"]
    ) == ["look_at_screen"]


def _speech_turn(**event_fields: object) -> HumanMessage:
    return HumanMessage(
        content="hello",
        id="m1",
        additional_kwargs={"meeting_event": {"kind": "speech", **event_fields}},
    )


def test_only_the_stamped_action_survives_the_tool_budget() -> None:
    stamped = [_speech_turn(meet_action="leave")]
    plain = [_speech_turn()]

    assert _pending_stamped_action("meet_leave", stamped)
    assert not _pending_stamped_action("meet_check_action", stamped)
    assert not _pending_stamped_action("meet_leave", plain)
    assert not _pending_stamped_action("look", stamped)


def test_a_stamped_action_is_exempt_only_until_it_fires_once() -> None:
    messages: list[AnyMessage] = [
        _speech_turn(meet_action="leave"),
        ToolMessage(content='{"code":"queued"}', tool_call_id="x1", name="meet_leave"),
    ]

    assert not _pending_stamped_action("meet_leave", messages)
    assert not _pending_stamped_action("meet_react", messages)
