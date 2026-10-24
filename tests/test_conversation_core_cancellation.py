import asyncio
from typing import Any, override

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.meeting_turns import build_meeting_turns
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import ToolCallingFakeModel
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


async def test_a_turn_cancelled_mid_tool_leaves_nothing_for_the_next_turn(
    journal: RecordingJournal,
) -> None:
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

    model = ToolCallingFakeModel(
        responses=[
            named_call("fast_lookup", "call-1"),
            named_call("hanging_lookup", "call-2"),
            AIMessage(content="second turn answer"),
        ]
    )
    agent = create_meeting_agent(model, tools=[fast_lookup, hanging_lookup])
    responder = MeetingResponder(agent, NoHistory(), journal=journal)
    audio = FakeReplyAudio()
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first question"))
    assert await asyncio.wait_for(hanging_begun.wait(), 2)
    assert fast_calls == ["demo"]
    await loop.ingress.receive_final(event("t2", "second question"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert audio.spoken == ["second turn answer"]
    assert fast_calls == ["demo"]
    assert hanging_calls == ["demo"]
    requests = model.seen[-1]
    assert not any(isinstance(message, ToolMessage) for message in requests)
    assert not any(isinstance(message, AIMessage) and message.tool_calls for message in requests)
    assert [message.text for message in requests if isinstance(message, HumanMessage)] == [
        "second question"
    ]
    assert journal.events("agent_failed") == []
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert cancelled["reason"] == "new_final"
    await loop.lifecycle.close()


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
        middleware=[ModelFallbackMiddleware(CountingModel(responses=[AIMessage(content="x")]))],
    )
    responder = MeetingResponder(agent, NoHistory(), journal=journal)
    audio = FakeReplyAudio()
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(entered.wait(), 2)
    await loop.ingress.receive_final(event("t2", "second"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert audio.spoken == ["primary answer"]
    assert fallback_calls == []
    assert journal.events("agent_failed") == []
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert cancelled["reason"] == "new_final"
    await loop.lifecycle.close()
