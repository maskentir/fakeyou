import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
    LoopInteraction,
    LoopTiming,
    cap_spoken_reply,
    conversation_thread_id,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.foundation.transcript import (
    CaptureContext,
    TranscriptEvent,
    transcript_event,
)
from fakeyou.observability.diagnostics import Diagnostics
from tests._conversation_support import AgentT, event
from tests.fakes import (
    FakeReplyAudio,
    FakeResponder,
    RecordingJournal,
    ToolCallingFakeModel,
    recall_call,
)


async def test_new_final_cancels_waiting_model_and_answers_newest_once(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.handle_transcript(event("t2", "second"))
    assert responder.cancelled.is_set()
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert responder.max_active == 1
    assert [[item.text for item in call] for call in responder.calls] == [
        ["first"],
        ["second"],
    ]
    assert audio.spoken == ["second"]
    [requested] = journal.events("agent_cancel_requested")
    assert (
        requested["request_id"],
        requested["transcript_id"],
        requested["generation"],
        requested["reason"],
    ) == ("t1:1", "t1", 1, "new_final")
    assert requested["duration_ms"] >= 0
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    assert (
        cancelled[0]["transcript_id"],
        cancelled[0]["generation"],
        cancelled[0]["reason"],
    ) == ("t1", 1, "new_final")
    assert cancelled[0]["duration_ms"] >= 0
    assert journal.events("agent_failed") == []
    await loop.close()


async def test_buffers_before_bind_and_ignores_duplicates_interims_and_empty_text() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    await loop.handle_transcript(event("provisional", finalized=False))
    await loop.handle_transcript(event("empty", "  "))
    await loop.handle_transcript(event("one", "kept"))
    await loop.handle_transcript(event("one", "kept"))
    assert responder.calls == []
    loop.bind(audio)
    await loop.wait_idle()
    assert audio.spoken == ["kept"]
    assert len(loop.events) == 1
    await loop.close()
    await loop.handle_transcript(event("after-close"))
    assert len(responder.calls) == 1


async def test_close_cancels_model_and_prevents_later_speech() -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await responder.started.wait()
    await loop.close()
    await loop.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []


async def test_model_timeout_is_observable_without_exposing_provider_error() -> None:
    loop = ConversationLoop(
        FakeResponder(asyncio.Event()), timing=LoopTiming(response_timeout_seconds=0.02)
    )
    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    await loop.close()


def test_cap_spoken_reply_keeps_short_replies() -> None:
    assert cap_spoken_reply("One sentence.") == "One sentence."
    assert cap_spoken_reply("One. Two.") == "One. Two."
    assert cap_spoken_reply("No final period") == "No final period"
    assert cap_spoken_reply("  ") == "  "


def test_cap_spoken_reply_cuts_after_two_sentences() -> None:
    assert cap_spoken_reply("One. Two. Three. Four. Five.") == "One. Two."
    assert cap_spoken_reply("Really? Yes! Okay then.") == "Really? Yes!"


def test_cap_spoken_reply_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="max_sentences"):
        cap_spoken_reply("One.", 0)


async def test_loop_speaks_capped_reply_head() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "One. Two. Three. Four."))
    await loop.wait_idle()
    assert audio.spoken == ["One. Two."]
    await loop.close()


async def test_observer_failure_preserves_model_and_speech() -> None:
    def broken(*args: Any) -> NoReturn:
        raise RuntimeError("private-provider-data")

    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, interaction=LoopInteraction(on_transition=broken))
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    assert len(audio.spoken) == 1
    await loop.close()


def test_frame_adapter_has_stable_event_identity_and_no_invented_participant() -> None:
    first = TranscriptionFrame("hello", "unverified-user", "2026-09-28T00:00:00Z", finalized=True)
    replay = TranscriptionFrame("hello", "unverified-user", "2026-09-28T00:00:00Z", finalized=True)
    next_utterance = TranscriptionFrame("hello", "", "2026-09-28T00:00:01Z", finalized=True)
    assert first.id != replay.id
    assert (
        transcript_event(first, "unknown", None, CaptureContext("")).id
        == transcript_event(replay, "p1", "e1", CaptureContext("")).id
    )
    assert (
        transcript_event(first, "unknown", None, CaptureContext("")).id
        != transcript_event(first, "unknown", None, CaptureContext("s0")).id
    )
    assert (
        transcript_event(first, "unknown", None, CaptureContext("")).id
        != transcript_event(next_utterance, "unknown", None, CaptureContext("")).id
    )
    assert transcript_event(first, "unknown", None, CaptureContext("")).participant_id == "unknown"
    assert transcript_event(first, "p1", "e1", CaptureContext("s0")).speaker_evidence_id == "e1"
    assert transcript_event(first, "p1", "e1", CaptureContext("s0")).stream_id == "s0"


def test_conversation_threads_include_all_session_identities() -> None:
    first = conversation_thread_id("owner-a", "twin", "meeting")
    assert first == conversation_thread_id("owner-a", "twin", "meeting")
    assert first != conversation_thread_id("owner-b", "twin", "meeting")
    assert first != conversation_thread_id("owner-a", "twin-2", "meeting")
    assert first != conversation_thread_id("owner-a", "twin", "meeting-2")


async def test_langgraph_adapter_uses_native_checkpoint_history_and_message_ids() -> None:
    agent = create_meeting_agent(
        FakeMessagesListChatModel(
            responses=[AIMessage(content="first answer"), AIMessage(content="second answer")]
        ),
        checkpointer=InMemorySaver(),
    )
    responder = LangGraphResponder(agent, "owner:twin:meeting")
    assert await responder.respond([event("one", "first")], lambda: None) == "first answer"
    assert await responder.respond([event("two", "second")], lambda: None) == "second answer"
    state = await agent.aget_state({"configurable": {"thread_id": "owner:twin:meeting"}})
    assert [item.content for item in state.values["messages"]] == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]
    assert [item.id for item in state.values["messages"] if isinstance(item, HumanMessage)] == [
        "one",
        "two",
    ]


@pytest.mark.parametrize(
    "message",
    [
        HumanMessage(content="private input"),
        ToolMessage(content="raw tool result", tool_call_id="call"),
    ],
)
async def test_adapter_never_speaks_user_or_tool_output(
    message: HumanMessage | ToolMessage,
) -> None:
    class Agent:
        async def ainvoke(self, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            return {"messages": [message]}

    responder = LangGraphResponder(cast(AgentT, Agent()), "thread")
    assert await responder.respond([event("one")], lambda: None) == ""


async def test_agent_error_raises_typed_provider_fault() -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    class FailingAgent:
        async def ainvoke(self, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
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
            return "ok"

    responder = JournaledResponder()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))

    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert responder.journal is None
    assert [json.loads(line)["event"] for line in lines] == ["agent_started", "agent_finished"]
    await loop.close()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Dr. Andi is here. We are ready. Let us start now.", "Dr. Andi is here. We are ready."),
        (
            "The value is 3.14. The project is ready. Next we start.",
            "The value is 3.14. The project is ready.",
        ),
        ("He said, “Ready.” We start. Wait.", "He said, “Ready.” We start."),
        ("One.Two.Three.", "One. Two."),
    ],
)
def test_voice_cap_uses_language_aware_sentence_boundaries(reply: str, expected: str) -> None:
    assert cap_spoken_reply(reply) == expected


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

    from fakeyou.observability.provider_faults import ProviderFault

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
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
    from fakeyou.observability.provider_faults import ProviderFault

    class FailingChatModel(ToolCallingFakeModel):
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


class GatedAudio(FakeReplyAudio):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.entered = asyncio.Event()
        self.attempts = 0

    async def speak(
        self, text: str, *, sentence_index: int | None = None, transcript_id: str | None = None
    ) -> None:
        self.attempts += 1
        self.entered.set()
        await self.gate.wait()
        await super().speak(text, sentence_index=sentence_index, transcript_id=transcript_id)


async def test_interim_cancels_pre_pcm_child_without_answering(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.handle_interim("hold on")
    assert not responder.cancelled.is_set()
    await loop.handle_interim("please hold on a moment")
    assert responder.cancelled.is_set()
    assert len(responder.calls) == 1
    assert audio.spoken == []
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "interim"
    assert (requested["request_id"], requested["transcript_id"]) == ("t1:1", "t1")
    assert journal.events("barge_in_detected") != []
    await loop.handle_transcript(event("t2", "second"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert audio.spoken == ["second"]
    assert journal.events("agent_failed") == []
    await loop.close()


async def test_duplicate_final_and_interim_do_not_reschedule(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.handle_transcript(event("t1", "first"))
    assert not responder.cancelled.is_set()
    assert len(responder.calls) == 1
    await loop.handle_interim("please hold on a moment")
    assert responder.cancelled.is_set()
    await loop.handle_interim("please hold on a moment")
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "interim"
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("t2", "second"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert audio.spoken == ["second"]
    assert [[item.text for item in call] for call in responder.calls] == [
        ["first"],
        ["second"],
    ]
    await loop.close()


async def test_close_cancels_active_turn_and_never_reschedules(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []
    assert loop.phase == "closed"
    await loop.close()
    await loop.handle_transcript(event("t2", "second"))
    assert len(responder.calls) == 1
    [requested] = journal.events("agent_cancel_requested")
    assert (
        requested["request_id"],
        requested["transcript_id"],
        requested["generation"],
        requested["reason"],
    ) == ("t1:1", "t1", 1, "close")
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    assert cancelled[0]["reason"] == "close"
    assert journal.events("agent_failed") == []


async def test_new_final_during_tts_drops_stale_audio(journal: RecordingJournal) -> None:
    gate = asyncio.Event()
    responder, audio = FakeResponder(), GatedAudio(gate)
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(audio.entered.wait(), 2)
    await loop.handle_transcript(event("t2", "second"))
    gate.set()
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert audio.attempts == 2
    assert audio.spoken == ["second"]
    assert loop.phase == "listening"
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "new_final"
    assert journal.events("agent_failed") == []
    await loop.close()


async def test_close_during_tts_speak_drops_audio_and_stays_closed(
    journal: RecordingJournal,
) -> None:
    gate = asyncio.Event()
    responder, audio = FakeResponder(), GatedAudio(gate)
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(audio.entered.wait(), 2)
    await loop.close()
    gate.set()
    assert audio.spoken == []
    assert loop.phase == "closed"
    await loop.close()
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "close"
    assert journal.events("agent_failed") == []


async def test_cancelled_request_ids_do_not_attach_to_next_transcript(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.handle_transcript(event("t2", "second"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    started = [row["request_id"] for row in journal.events("agent_started")]
    assert started == ["t1:1", "t2:2"]
    finished = [row["request_id"] for row in journal.events("agent_finished")]
    assert finished == ["t2:2"]
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert (cancelled["transcript_id"], cancelled["generation"]) == ("t1", 1)
    body = "\n".join(journal.lines)
    assert "first" not in body
    assert "second" not in body
    await loop.close()


async def test_cancel_between_tool_call_and_result_keeps_usable_checkpoint(
    journal: RecordingJournal,
) -> None:
    from langchain_core.messages import HumanMessage, ToolMessage
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


def test_transcript_event_and_record_carry_capture_ms(tmp_path: Path) -> None:
    from fakeyou.persistence.record import MeetingRecord, TranscriptEntry, load_meeting_record

    event = transcript_event(
        TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00", finalized=True),
        "a",
        None,
        CaptureContext("s0", capture_ms=55.5),
    )
    assert event.capture_ms == 55.5
    assert (
        transcript_event(
            TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00", finalized=True),
            "a",
            None,
            CaptureContext("s0"),
        ).capture_ms
        == 0.0
    )
    record = MeetingRecord(tmp_path / "record.jsonl")
    record.add_transcript(event)
    record.close()
    [entry] = load_meeting_record(tmp_path / "record.jsonl")
    assert isinstance(entry, TranscriptEntry)
    assert entry.capture_ms == 55.5


async def test_adapter_uses_native_text_blocks_and_current_tool_callbacks() -> None:
    observed = []

    class Agent:
        async def ainvoke(self, request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            await config["callbacks"][0].on_tool_start({}, "", run_id=None)
            return {
                "messages": [
                    AIMessage(
                        content=[
                            {"type": "reasoning", "text": "private thought"},
                            {"type": "text", "text": "spoken"},
                        ]
                    )
                ]
            }

    responder = LangGraphResponder(cast(AgentT, Agent()), "thread")
    assert await responder.respond([event("one")], lambda: observed.append("tool")) == "spoken"
    assert observed == ["tool"]


async def test_responder_reports_tool_results_to_journal_and_callback(
    journal: RecordingJournal,
) -> None:

    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
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


async def test_cancelled_model_request_does_not_use_fallback(journal: RecordingJournal) -> None:
    from langchain.agents.middleware import ModelFallbackMiddleware

    entered = asyncio.Event()
    attempts: list[int] = []
    fallback_calls: list[int] = []

    class HangingModel(ToolCallingFakeModel):
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
