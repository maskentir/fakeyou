import asyncio
from types import SimpleNamespace
from typing import Any, NoReturn, cast, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
)
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import ToolCallingFakeModel
from tests._conversation_support import AgentT, event
from tests._persistence_fakes import RecordingJournal


async def test_cancel_between_tool_call_and_result_keeps_usable_checkpoint(
    journal: RecordingJournal,
) -> None:
    from langchain_core.messages import ToolMessage
    from langchain_core.tools import tool

    fast_calls: list[str] = []
    hanging_calls: list[str] = []
    hanging_begun = asyncio.Event()
    hanging_gate = asyncio.Event()

    @tool(description="Fast lookup.")
    async def fast_lookup(query: str) -> str:
        fast_calls.append(query)
        return "fast result"

    @tool(description="Hanging lookup.")
    async def hanging_lookup(query: str) -> str:
        hanging_calls.append(query)
        hanging_begun.set()
        await hanging_gate.wait()
        return "late result"

    def named_call(name: str, call_id: str) -> AIMessage:
        calls = [{"name": name, "args": {"query": "demo"}, "id": call_id, "type": "tool_call"}]
        return AIMessage(content="", tool_calls=calls)

    agent = create_meeting_agent(
        ToolCallingFakeModel(
            responses=[
                named_call("fast_lookup", "call-1"),
                named_call("hanging_lookup", "call-2"),
                AIMessage(content="second turn answer"),
            ]
        ),
        tools=[fast_lookup, hanging_lookup],
        checkpointer=InMemorySaver(),
    )
    responder = LangGraphResponder(agent, "checkpoint-cancel-thread", journal=journal)
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first question"))
    assert await asyncio.wait_for(hanging_begun.wait(), 2)
    assert fast_calls == ["demo"]
    await loop.handle_transcript(event("t2", "second question"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert audio.spoken == ["second turn answer"]
    assert fast_calls == ["demo"]
    assert hanging_calls == ["demo"]
    state = await agent.aget_state({"configurable": {"thread_id": "checkpoint-cancel-thread"}})
    answered = {
        message.tool_call_id
        for message in state.values["messages"]
        if isinstance(message, ToolMessage)
    }
    orphans = [
        call["id"]
        for message in state.values["messages"]
        if isinstance(message, AIMessage) and message.tool_calls
        for call in message.tool_calls
        if call["id"] not in answered
    ]
    assert orphans == []
    assert [
        message.content for message in state.values["messages"] if isinstance(message, HumanMessage)
    ] == ["first question", "second question"]
    assert journal.events("agent_failed") == []
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert cancelled["reason"] == "new_final"
    await loop.close()


async def test_cancelled_model_request_does_not_use_fallback(journal: RecordingJournal) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    entered = asyncio.Event()
    attempts: list[int] = []
    fallback_calls: list[int] = []

    class HangingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            attempts.append(1)
            if len(attempts) == 1:
                entered.set()
                await asyncio.Event().wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    class CountingModel(ToolCallingFakeModel):
        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            fallback_calls.append(1)
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    agent = create_meeting_agent(
        HangingModel(responses=[AIMessage(content="primary answer")]),
        checkpointer=InMemorySaver(),
        middleware=[ModelFallbackMiddleware(CountingModel(responses=[AIMessage(content="x")]))],
    )
    responder = LangGraphResponder(agent, "fallback-cancel-thread", journal=journal)
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(entered.wait(), 2)
    await loop.handle_transcript(event("t2", "second"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert audio.spoken == ["primary answer"]
    assert fallback_calls == []
    assert journal.events("agent_failed") == []
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert cancelled["reason"] == "new_final"
    await loop.close()


class _CheckpointAgent:
    def __init__(
        self,
        state: Any = None,
        *,
        get_error: BaseException | None = None,
        update_error: BaseException | None = None,
    ) -> None:
        self._state = state
        self._get_error = get_error
        self._update_error = update_error
        self.updated: list[Any] = []
        self.entered = asyncio.Event()

    async def ainvoke(self, payload: Any, config: Any = None) -> NoReturn:
        del config
        del payload
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("ainvoke must be cancelled")

    async def aget_state(self, config: dict[str, Any]) -> Any:
        del config
        if self._get_error is not None:
            raise self._get_error
        return self._state

    async def aupdate_state(
        self, config: dict[str, Any], values: dict[str, Any], as_node: str | None = None
    ) -> None:
        del as_node
        del config
        if self._update_error is not None:
            raise self._update_error
        self.updated.append(values)


def _pending_tool_state() -> Any:
    message = AIMessage(
        content="working",
        tool_calls=[{"name": "recall", "args": {}, "id": "call-1", "type": "tool_call"}],
    )
    return SimpleNamespace(values={"messages": [message]})


async def _cancel_mid_turn(agent: _CheckpointAgent) -> asyncio.Task[str]:
    responder = LangGraphResponder(cast(AgentT, agent), "thread")
    turn = asyncio.create_task(responder.respond([event("t1", "hi")], lambda: None))
    assert await asyncio.wait_for(agent.entered.wait(), 2)
    turn.cancel()
    return turn


async def test_cancelled_turn_answers_orphan_tool_calls() -> None:
    agent = _CheckpointAgent(_pending_tool_state())

    turn = await _cancel_mid_turn(agent)

    with pytest.raises(asyncio.CancelledError):
        await turn
    [update] = agent.updated
    [answer] = update["messages"]
    assert isinstance(answer, ToolMessage)
    assert answer.tool_call_id == "call-1"


async def test_cancelled_turn_ignores_checkpoint_read_errors() -> None:
    agent = _CheckpointAgent(get_error=TimeoutError("store slow"))

    turn = await _cancel_mid_turn(agent)

    with pytest.raises(asyncio.CancelledError):
        await turn
    assert agent.updated == []


async def test_cancelled_turn_ignores_checkpoint_write_errors() -> None:
    agent = _CheckpointAgent(_pending_tool_state(), update_error=TimeoutError("store slow"))

    turn = await _cancel_mid_turn(agent)

    with pytest.raises(asyncio.CancelledError):
        await turn
    assert agent.updated == []


async def test_cancelled_turn_propagates_unexpected_read_error() -> None:
    turn = await _cancel_mid_turn(_CheckpointAgent(get_error=ValueError("bug")))

    with pytest.raises(ValueError, match="bug"):
        await turn


async def test_cancelled_turn_propagates_unexpected_write_error() -> None:
    turn = await _cancel_mid_turn(
        _CheckpointAgent(_pending_tool_state(), update_error=ValueError("bug"))
    )

    with pytest.raises(ValueError, match="bug"):
        await turn
