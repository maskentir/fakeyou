import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any, cast, override

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
    LoopInteraction,
    LoopTiming,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder, ToolCallingFakeModel, recall_call
from tests._conversation_support import AgentT, event
from tests._persistence_fakes import RecordingJournal


async def test_model_timeout_is_observable_without_exposing_provider_error() -> None:
    loop = ConversationLoop(
        FakeResponder(asyncio.Event()), timing=LoopTiming(response_timeout_seconds=0.02)
    )
    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    await loop.close()


class _BuggyResponder(FakeResponder):
    @override
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str:
        raise ValueError("bug")


async def test_responder_bug_fails_turn_with_conversation_error() -> None:
    journal = RecordingJournal("meeting-bug")
    reporter = FaultReporter(journal=journal)
    loop = ConversationLoop(_BuggyResponder(), diagnostics=Diagnostics(faults=reporter))
    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("t1", "hello"))
    with pytest.raises(ConversationError, match="conversation failed") as caught:
        await loop.wait_idle()
    assert isinstance(caught.value.__cause__, ValueError)
    assert reporter.total() >= 1
    await loop.close()


async def test_agent_error_raises_typed_provider_fault() -> None:

    class FailingAgent:
        async def ainvoke(self, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            del config
            del request
            raise RuntimeError("agent exploded")

    responder = LangGraphResponder(cast(AgentT, FailingAgent()), "thread")
    with pytest.raises(ProviderFault) as caught:
        await responder.respond([event("one")], lambda: None)

    assert caught.value.source == "agent"
    assert caught.value.kind == "unknown"
    assert caught.value.__cause__ is not None


async def test_responder_error_still_fails_loop() -> None:
    class Broken:
        async def respond(
            self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
        ) -> str:
            del events
            del on_tool_start
            raise RuntimeError("model exploded")

    loop = ConversationLoop(Broken())
    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    assert loop.phase == "failed"
    await loop.close()


async def test_loop_journals_turn_without_modifying_injected_responder(
    journal: RecordingJournal,
) -> None:
    from dataclasses import dataclass

    lines = journal.lines

    @dataclass(frozen=True)
    class JournaledResponder:
        journal: object | None = None

        async def respond(
            self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
        ) -> str:
            del events
            del on_tool_start
            return "ok"

    responder = JournaledResponder()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))

    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert responder.journal is None
    assert [json.loads(line)["event"] for line in lines] == ["agent_started", "agent_finished"]
    await loop.close()


def _recall_then_reply() -> ToolCallingFakeModel:
    return ToolCallingFakeModel(
        responses=[
            recall_call({"query": "demo"}, id="call-1"),
            AIMessage(content="The demo is on Friday."),
        ]
    )


async def test_responder_journals_tool_failures(
    journal: RecordingJournal,
) -> None:

    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        del query
        raise RuntimeError("graph down")

    lines = journal.lines
    agent = create_meeting_agent(
        _recall_then_reply(), tools=[recall_meeting_context], checkpointer=InMemorySaver()
    )
    responder = LangGraphResponder(agent, "thread", journal=journal)

    with pytest.raises(ProviderFault):
        await responder.respond([event("one", "When is the demo?")], lambda: None)

    [failed] = [json.loads(line) for line in lines if '"tool_failed"' in line]
    assert (failed["tool"], failed["error"]) == ("recall_meeting_context", "RuntimeError")


async def test_model_and_tool_events_carry_their_transcript(journal: RecordingJournal) -> None:
    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        del query
        return "From owner note: The demo is on Friday"

    agent = create_meeting_agent(
        ToolCallingFakeModel(
            responses=[
                recall_call({"query": "demo"}, id="call-1"),
                AIMessage(content="The demo is on Friday."),
                AIMessage(content="Tuesday at ten."),
            ]
        ),
        tools=[recall_meeting_context],
        checkpointer=InMemorySaver(),
    )
    responder = LangGraphResponder(agent, "thread", journal=journal)

    assert (
        await responder.respond([event("t1", "When is the demo?")], lambda: None)
        == "The demo is on Friday."
    )
    assert await responder.respond([event("t2", "What time?")], lambda: None) == "Tuesday at ten."

    assert [row["transcript_id"] for row in journal.events("request_started")] == ["t1", "t1", "t2"]
    assert [row["transcript_id"] for row in journal.events("request_finished")] == [
        "t1",
        "t1",
        "t2",
    ]
    [tool_started] = journal.events("tool_started")
    assert (tool_started["tool"], tool_started["transcript_id"]) == ("recall_meeting_context", "t1")
    [tool_finished] = journal.events("tool_finished")
    assert (tool_finished["tool"], tool_finished["transcript_id"]) == (
        "recall_meeting_context",
        "t1",
    )
    body = "\n".join(journal.lines)
    assert "When is the demo?" not in body
    assert "The demo is on Friday" not in body
    assert "Tuesday at ten" not in body
    assert "owner note" not in body


async def test_failed_model_request_keeps_transcript_and_error_type(
    journal: RecordingJournal,
) -> None:

    class FailingChatModel(ToolCallingFakeModel):
        @override
        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: AsyncCallbackManagerForLLMRun | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            raise RuntimeError("model exploded")

    agent = create_meeting_agent(
        FailingChatModel(responses=[AIMessage(content="unused")]),
        checkpointer=InMemorySaver(),
    )
    responder = LangGraphResponder(agent, "thread", journal=journal)

    with pytest.raises(ProviderFault):
        await responder.respond([event("t1", "When is the demo?")], lambda: None)

    [started] = journal.events("request_started")
    assert started["transcript_id"] == "t1"
    [failed] = journal.events("request_failed")
    assert (failed["transcript_id"], failed["error"]) == ("t1", "RuntimeError")
    assert "When is the demo?" not in "\n".join(journal.lines)


async def test_cancelled_tool_run_keeps_transcript_without_content(
    journal: RecordingJournal,
) -> None:
    from langchain_core.tools import tool

    gate = asyncio.Event()
    begun = asyncio.Event()

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        del query
        await gate.wait()
        return "a late answer"

    agent = create_meeting_agent(
        ToolCallingFakeModel(responses=[recall_call({"query": "demo"}, id="call-1")]),
        tools=[recall_meeting_context],
        checkpointer=InMemorySaver(),
    )
    responder = LangGraphResponder(agent, "thread", journal=journal)
    task = asyncio.create_task(
        responder.respond([event("t1", "When is the demo?")], lambda: begun.set())
    )

    assert await asyncio.wait_for(begun.wait(), 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [row["transcript_id"] for row in journal.events("request_started")] == ["t1"]
    [started_tool] = journal.events("tool_started")
    assert started_tool["transcript_id"] == "t1"
    assert journal.events("tool_finished") == []
    body = "\n".join(journal.lines)
    assert "When is the demo?" not in body
    assert "a late answer" not in body


async def test_responder_reports_tool_results_to_journal_and_callback(
    journal: RecordingJournal,
) -> None:

    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        del query
        return (
            "From owner note on 2026-09-30 06:40 UTC: The demo is on Friday\n"
            "From Google Meet m1 transcript on 2026-09-23 03:10 UTC: Budi is the PIC"
        )

    results: list[tuple[str, str, float]] = []
    agent = create_meeting_agent(
        _recall_then_reply(), tools=[recall_meeting_context], checkpointer=InMemorySaver()
    )
    responder = LangGraphResponder(
        agent,
        "thread",
        journal=journal,
        on_tool_result=lambda *result: results.append(result),
    )

    assert (
        await responder.respond([event("one", "When is the demo?")], lambda: None)
        == "The demo is on Friday."
    )

    [(tool_name, output, duration_ms)] = results
    assert tool_name == "recall_meeting_context"
    assert (
        output.splitlines()[0] == "From owner note on 2026-09-30 06:40 UTC: The demo is on Friday"
    )
    assert duration_ms >= 0
    [finished] = journal.events("tool_finished")
    assert finished["tool"] == "recall_meeting_context"
    assert finished["output_lines"] == 2
    assert "owner note" not in json.dumps(finished)


async def test_transition_observer_error_is_recorded_as_fault() -> None:
    journal = RecordingJournal("meeting-transition")
    reporter = FaultReporter(journal=journal)

    def boom(previous: Any, phase: Any, detail: str) -> None:
        del detail
        del phase
        del previous
        raise ValueError("bug")

    audio = FakeReplyAudio()
    loop = ConversationLoop(
        FakeResponder(),
        interaction=LoopInteraction(on_transition=boom),
        diagnostics=Diagnostics(faults=reporter),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()

    (summary,) = reporter.summaries()
    assert summary.operation == "transition_observer"
    assert audio.spoken == ["hello"]
    await loop.close()
