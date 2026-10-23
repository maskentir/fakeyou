from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    MeetingResponder,
)
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.meeting_turns import TurnGeneration, build_meeting_turns
from fakeyou.foundation.transcript import (
    CaptureContext,
    TranscriptEvent,
    TranscriptUpdate,
    transcript_event,
)
from fakeyou.foundation.triggers import current_trigger, is_history
from fakeyou.persistence.record import MeetingRecord
from tests._conversation_fakes import ToolCallingFakeModel
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


async def test_each_turn_is_built_from_what_the_meeting_record_holds(tmp_path: Path) -> None:
    model = ToolCallingFakeModel(
        responses=[AIMessage(content="first answer"), AIMessage(content="second answer")]
    )
    record = MeetingRecord(tmp_path / "record.jsonl")
    responder = MeetingResponder(create_meeting_agent(model), record)
    first, second, aside = event("one", "first"), event("two", "second"), event("x", "aside")

    record.add_transcript(first)
    assert await responder.respond([first], lambda: None) == "first answer"
    record.add_twin_speech("First answer was cut")
    record.add_twin_speech("short", interrupted=True)
    record.add_transcript(aside)
    record.add_transcript(second)
    assert await responder.respond([second], lambda: None) == "second answer"
    record.close()

    seen = model.seen[-1][1:]
    assert [(type(item).__name__, item.text) for item in seen] == [
        ("HumanMessage", "first"),
        ("AIMessage", "First answer was cut short —"),
        ("HumanMessage", "aside"),
        ("HumanMessage", "second"),
    ]
    assert [item.id for item in seen if isinstance(item, HumanMessage)] == ["one", "x", "two"]
    assert current_trigger(model.seen[-1])[1] == [model.seen[-1][-1]]
    assert is_history(model.seen[-1][1])


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

    responder = MeetingResponder(cast(AgentT, Agent()), NoHistory())
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

    responder = MeetingResponder(cast(AgentT, Agent()), NoHistory())
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
        build_meeting_turns(
            _StreamOnlyResponder(), generation=TurnGeneration(sentence_streaming=False)
        )


def test_stream_only_responder_is_accepted_when_sentence_streaming_is_on() -> None:
    loop = build_meeting_turns(
        _StreamOnlyResponder(), generation=TurnGeneration(sentence_streaming=True)
    )

    assert loop.lifecycle.phase == "listening"
