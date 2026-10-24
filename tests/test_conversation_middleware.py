from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.exceptions import ModelAuthenticationError, ModelRateLimitError
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import MeetingResponder
from fakeyou.conversation.meeting_messages import request_messages
from fakeyou.conversation.middleware import (
    SPEECH_MARK,
    HistoryTrimMiddleware,
    OrphanToolCallMiddleware,
    SpeakerAttributionMiddleware,
    SpeechMark,
    attributed,
    estimate_message_tokens,
    patch_orphan_tool_calls,
    retry_before_speech,
    trim_messages_to_budget,
)
from fakeyou.persistence.record import MeetingRecord, TranscriptEntry
from tests._conversation_fakes import ToolCallingFakeModel, recall_call
from tests._conversation_support import event


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
    assert estimate_message_tokens(seen[1:]) <= 100
    assert seen[-1].content == "new question"
    assert all(
        "xxxxx" not in str(message.content) and "yyyyy" not in str(message.content)
        for message in seen
    )
    state = await agent.aget_state({"configurable": {"thread_id": "trim-thread"}})
    assert len(state.values["messages"]) == 4


async def test_the_model_reads_who_said_each_line_while_the_record_keeps_plain_text(
    tmp_path: Path,
) -> None:
    model = ToolCallingFakeModel(responses=[AIMessage(content="Noted.")])
    record = MeetingRecord(tmp_path / "record.jsonl")
    earlier = replace(event("one", "Budget is fine."), participant_id="speaker-a")
    now = replace(event("two", "Do you agree?"), participant_id="speaker-b", display_claim="Maya")
    record.add_transcript(earlier)
    record.add_twin_speech("I heard that.")
    record.add_transcript(now)
    agent = create_meeting_agent(model, middleware=[SpeakerAttributionMiddleware()])

    reply = await MeetingResponder(agent, record).respond([now], lambda: None)
    assert reply.text == "Noted."
    record.close()

    assert [message.text for message in model.seen[-1][1:]] == [
        "Speaker A with unverified identity: Budget is fine.",
        "I heard that.",
        "Speaker B, shown as Maya, unverified: Do you agree?",
    ]
    assert [entry.text for entry in record.entries() if isinstance(entry, TranscriptEntry)] == [
        "Budget is fine.",
        "Do you agree?",
    ]


def test_attribution_keeps_attached_images_and_leaves_other_messages_alone() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    spoken = request_messages([event("one", "See this?")])[0]
    with_image = spoken.model_copy(
        update={"content": [{"type": "text", "text": "See this?"}, image]}
    )

    labelled = attributed(with_image)

    assert labelled.content == [
        {"type": "text", "text": "Speaker with unverified identity:"},
        {"type": "text", "text": "See this?"},
        image,
    ]
    assert attributed(HumanMessage(content="owner chat")) == HumanMessage(content="owner chat")
    assert attributed(AIMessage(content="reply")) == AIMessage(content="reply")


def test_a_transient_model_error_is_retried_before_the_twin_speaks() -> None:
    assert retry_before_speech(ModelRateLimitError("slow down")) is True
    assert retry_before_speech(ModelAuthenticationError("bad key")) is False
    assert retry_before_speech(RuntimeError("model exploded")) is False


def test_nothing_is_retried_once_the_twin_has_started_speaking() -> None:
    mark = SpeechMark(started=True)
    token = SPEECH_MARK.set(mark)
    try:
        assert retry_before_speech(ModelRateLimitError("slow down")) is False
    finally:
        SPEECH_MARK.reset(token)


async def test_the_meeting_agent_retries_a_rate_limited_model_once() -> None:
    class FlakyModel(ToolCallingFakeModel):
        attempts: int = 0

        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            self.attempts += 1
            if self.attempts == 1:
                raise ModelRateLimitError("slow down")
            return await super()._agenerate(messages, stop, run_manager, **kwargs)

    model = FlakyModel(responses=[AIMessage(content="Noted.")])
    agent = create_meeting_agent(model)

    result = await agent.ainvoke({"messages": [HumanMessage("hello")]})

    assert model.attempts == 2
    assert result["messages"][-1].text == "Noted."
