from collections.abc import Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.middleware import (
    HistoryTrimMiddleware,
    OrphanToolCallMiddleware,
    estimate_message_tokens,
    patch_orphan_tool_calls,
    trim_messages_to_budget,
)
from tests._conversation_fakes import ToolCallingFakeModel, recall_call


def _orphan_call(call_id: str) -> dict[str, Any]:
    return {"name": "lookup", "args": {}, "id": call_id, "type": "tool_call"}


def _tool_ids(messages: Sequence[BaseMessage]) -> tuple[list[str | None], set[str]]:
    calls = [
        call["id"]
        for message in messages
        if isinstance(message, AIMessage) and message.tool_calls
        for call in message.tool_calls
    ]
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    return calls, answered


def test_patch_orphan_tool_calls_inserts_missing_results() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="question"),
        AIMessage(content="", tool_calls=[_orphan_call("call-9")]),
        HumanMessage(content="next"),
    ]

    patched = patch_orphan_tool_calls(messages)

    assert [type(message).__name__ for message in patched] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "HumanMessage",
    ]
    filler = patched[2]
    assert isinstance(filler, ToolMessage)
    assert filler.tool_call_id == "call-9"
    assert filler.content != ""


def test_patch_orphan_tool_calls_leaves_answered_calls_unchanged() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="question"),
        recall_call({"query": "demo"}),
        ToolMessage(content="result", tool_call_id="call-1"),
    ]

    assert patch_orphan_tool_calls(messages) == messages


def test_patch_orphan_tool_calls_ignores_text_only_history() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="question"),
        AIMessage(content="answer"),
    ]

    assert patch_orphan_tool_calls(messages) == messages


async def test_orphan_middleware_answers_stale_calls_on_a_chat_thread() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="Fresh answer.")])
    agent = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        middleware=[OrphanToolCallMiddleware()],
    )
    await agent.aupdate_state(
        {"configurable": {"thread_id": "orphan-nonstream"}},
        {
            "messages": [
                HumanMessage(content="old question"),
                AIMessage(content="", tool_calls=[_orphan_call("call-old")]),
            ]
        },
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="new question")]},
        {"configurable": {"thread_id": "orphan-nonstream"}},
    )

    assert result["messages"][-1].text == "Fresh answer."
    calls, answered = _tool_ids(model.seen[-1])
    assert calls != []
    assert all(call in answered for call in calls)
    stored = await agent.aget_state({"configurable": {"thread_id": "orphan-nonstream"}})
    stored_calls, stored_answered = _tool_ids(stored.values["messages"])
    assert any(call not in stored_answered for call in stored_calls)


def test_estimate_message_tokens_counts_text_heuristic() -> None:
    assert estimate_message_tokens([HumanMessage(content="a" * 40)]) == 12
    assert estimate_message_tokens([HumanMessage(content="")]) == 2
    assert (
        estimate_message_tokens([HumanMessage(content=[{"type": "text", "text": "b" * 40}])]) == 12
    )


def test_trim_messages_to_budget_keeps_system_and_recent() -> None:
    messages: list[AnyMessage] = [
        SystemMessage(content="sys"),
        HumanMessage(content="a" * 400),
        AIMessage(content="b" * 400),
        HumanMessage(content="latest"),
    ]

    trimmed = trim_messages_to_budget(messages, max_tokens=20)

    assert trimmed == [messages[0], messages[-1]]
    assert estimate_message_tokens(trimmed) <= 20


def test_trim_messages_to_budget_leaves_short_history_unchanged() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="question"),
        AIMessage(content="answer"),
    ]

    assert trim_messages_to_budget(messages, max_tokens=1000) == messages


def test_trim_drops_answers_whose_call_was_trimmed() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="o" * 400),
        AIMessage(content="", tool_calls=[_orphan_call("call-1")]),
        ToolMessage(content="ok", tool_call_id="call-1"),
        HumanMessage(content="latest"),
    ]

    trimmed = trim_messages_to_budget(messages, max_tokens=6)

    assert trimmed == [messages[-1]]


def test_trim_patches_calls_whose_answer_was_trimmed() -> None:
    messages: list[AnyMessage] = [
        HumanMessage(content="o" * 400),
        AIMessage(content="", tool_calls=[_orphan_call("call-1")]),
        HumanMessage(content="latest"),
    ]

    trimmed = trim_messages_to_budget(messages, max_tokens=10)

    calls, answered = _tool_ids(trimmed)
    assert calls == ["call-1"]
    assert answered == {"call-1"}


async def test_history_trim_limits_a_chat_request_but_not_its_thread() -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="Fresh answer.")])
    agent = create_meeting_agent(
        model,
        checkpointer=InMemorySaver(),
        middleware=[HistoryTrimMiddleware(max_tokens=100)],
    )
    await agent.aupdate_state(
        {"configurable": {"thread_id": "trim-thread"}},
        {
            "messages": [
                HumanMessage(content="old question " + "x" * 400),
                AIMessage(content="old answer " + "y" * 400),
            ]
        },
    )
    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="new question")]},
        {"configurable": {"thread_id": "trim-thread"}},
    )

    assert result["messages"][-1].text == "Fresh answer."
    seen = model.seen[-1]
    assert estimate_message_tokens(seen) <= 200
    assert seen[-1].content == "new question"
    assert all(
        "xxxxx" not in str(message.content) and "yyyyy" not in str(message.content)
        for message in seen
    )
    state = await agent.aget_state({"configurable": {"thread_id": "trim-thread"}})
    assert len(state.values["messages"]) == 4
