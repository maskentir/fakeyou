from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    ConversationLoop,
    LangGraphResponder,
    LoopGeneration,
    conversation_thread_id,
)
from fakeyou.foundation.transcript import (
    CaptureContext,
    TranscriptEvent,
    TranscriptUpdate,
    transcript_event,
)
from tests._conversation_support import AgentT, event


def test_frame_adapter_has_stable_event_identity_and_no_invented_participant() -> None:
    first = TranscriptUpdate(text="hello", final=True, timestamp="2026-09-28T00:00:00Z")
    replay = TranscriptUpdate(text="hello", final=True, timestamp="2026-09-28T00:00:00Z")
    next_utterance = TranscriptUpdate(text="hello", final=True, timestamp="2026-09-28T00:00:01Z")
    assert (
        transcript_event(first, "a", None, CaptureContext("")).id
        != transcript_event(next_utterance, "a", None, CaptureContext("")).id
    )
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
            del config
            del request
            return {"messages": [message]}

    responder = LangGraphResponder(cast(AgentT, Agent()), "thread")
    assert await responder.respond([event("one")], lambda: None) == ""


def test_transcript_event_and_record_carry_capture_ms(tmp_path: Path) -> None:
    from fakeyou.persistence.record import MeetingRecord, TranscriptEntry, load_meeting_record

    event = transcript_event(
        TranscriptUpdate(text="hi", final=True, timestamp="2026-10-06T01:00:01+00:00"),
        "a",
        None,
        CaptureContext("s0", capture_ms=55.5),
    )
    assert event.capture_ms == 55.5
    assert (
        transcript_event(
            TranscriptUpdate(text="hi", final=True, timestamp="2026-10-06T01:00:01+00:00"),
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
            del request
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


class _StreamOnlyResponder:
    def stream_sentences(
        self,
        events: Sequence[TranscriptEvent],
        on_tool_start: Callable[[], None],
        *,
        request_id: str,
    ) -> AsyncIterator[str]:
        del events
        del on_tool_start
        del request_id
        return self._generate()

    async def _generate(self) -> AsyncIterator[str]:
        yield "Hello."


def test_stream_only_responder_is_rejected_when_sentence_streaming_is_off() -> None:
    with pytest.raises(TypeError, match="cannot answer without sentence streaming"):
        ConversationLoop(
            _StreamOnlyResponder(), generation=LoopGeneration(sentence_streaming=False)
        )


def test_stream_only_responder_is_accepted_when_sentence_streaming_is_on() -> None:
    loop = ConversationLoop(
        _StreamOnlyResponder(), generation=LoopGeneration(sentence_streaming=True)
    )

    assert loop.phase == "listening"
