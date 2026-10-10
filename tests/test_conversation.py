import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    BARE_ACK_VOCABULARY,
    ConversationLoop,
    LangGraphResponder,
    StreamingLangGraphResponder,
    _is_bare_ack,
    _request_messages,
    cap_spoken_reply,
    conversation_thread_id,
)
from fakeyou.conversation.session import ConversationError
from fakeyou.foundation.transcript import TranscriptEvent, transcript_event
from fakeyou.foundation.utterance_quality import describe_word_diff, normalize_words
from fakeyou.memory.memory import create_recall_error_middleware
from tests.fakes import FakeClock, FakeReplyAudio, FakeResponder, ToolCallingFakeModel, recall_call


def event(id, text="hello", finalized=True, participant_id="unknown", speaker_evidence_id=None):
    return TranscriptEvent(
        id,
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        text,
        finalized,
        participant_id=participant_id,
        speaker_evidence_id=speaker_evidence_id,
    )


async def test_new_final_cancels_waiting_model_and_answers_newest_once(journal):
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
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


async def test_buffers_before_bind_and_ignores_duplicates_interims_and_empty_text():
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


async def test_close_cancels_model_and_prevents_later_speech():
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await responder.started.wait()
    await loop.close()
    await loop.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []


async def test_model_timeout_is_observable_without_exposing_provider_error():
    loop = ConversationLoop(FakeResponder(asyncio.Event()), response_timeout_seconds=0.02)
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


async def test_observer_failure_preserves_model_and_speech():
    def broken(*args):
        raise RuntimeError("private-provider-data")

    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, on_transition=broken)
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    assert len(audio.spoken) == 1
    await loop.close()


def test_frame_adapter_has_stable_event_identity_and_no_invented_participant():
    first = TranscriptionFrame("hello", "unverified-user", "2026-09-28T00:00:00Z", finalized=True)
    replay = TranscriptionFrame("hello", "unverified-user", "2026-09-28T00:00:00Z", finalized=True)
    next_utterance = TranscriptionFrame("hello", "", "2026-09-28T00:00:01Z", finalized=True)
    assert first.id != replay.id
    assert (
        transcript_event(first, "unknown", None, "").id
        == transcript_event(replay, "p1", "e1", "").id
    )
    assert (
        transcript_event(first, "unknown", None, "").id
        != transcript_event(first, "unknown", None, "s0").id
    )
    assert (
        transcript_event(first, "unknown", None, "").id
        != transcript_event(next_utterance, "unknown", None, "").id
    )
    assert transcript_event(first, "unknown", None, "").participant_id == "unknown"
    assert transcript_event(first, "p1", "e1", "s0").speaker_evidence_id == "e1"
    assert transcript_event(first, "p1", "e1", "s0").stream_id == "s0"


def test_conversation_threads_include_all_session_identities():
    first = conversation_thread_id("owner-a", "twin", "meeting")
    assert first == conversation_thread_id("owner-a", "twin", "meeting")
    assert first != conversation_thread_id("owner-b", "twin", "meeting")
    assert first != conversation_thread_id("owner-a", "twin-2", "meeting")
    assert first != conversation_thread_id("owner-a", "twin", "meeting-2")


async def test_langgraph_adapter_uses_native_checkpoint_history_and_message_ids():
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
async def test_adapter_never_speaks_user_or_tool_output(message):
    class Agent:
        async def ainvoke(self, request, config):
            return {"messages": [message]}

    responder = LangGraphResponder(Agent(), "thread")
    assert await responder.respond([event("one")], lambda: None) == ""


async def test_adapter_uses_native_text_blocks_and_current_tool_callbacks():
    observed = []

    class Agent:
        async def ainvoke(self, request, config):
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

    responder = LangGraphResponder(Agent(), "thread")
    assert await responder.respond([event("one")], lambda: observed.append("tool")) == "spoken"
    assert observed == ["tool"]


async def test_wake_mode_ignores_unaddressed_transcripts_but_records_them():
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(responder, wake_phrases=["hey fakeyou"], clock=clock)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "our project status"))
    await loop.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    assert [item.text for item in loop.events] == ["our project status"]
    await loop.close()


async def test_wake_phrase_activates_window_case_insensitively():
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder, wake_phrases=["hey fakeyou"], activation_seconds=30, clock=clock
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "HEY Fakeyou, any update?"))
    await loop.wait_idle()
    clock.now += 10
    await loop.handle_transcript(event("two", "continue"))
    await loop.wait_idle()
    clock.now += 31
    await loop.handle_transcript(event("three", "still listening?"))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [
        ["HEY Fakeyou, any update?"],
        ["continue"],
    ]
    assert len(audio.spoken) == 2
    await loop.close()


async def test_wake_phrase_inside_window_extends_activation():
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder, wake_phrases=["hey fakeyou"], activation_seconds=30, clock=clock
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "hey fakeyou hello"))
    await loop.wait_idle()
    clock.now += 25
    await loop.handle_transcript(event("two", "hey fakeyou again"))
    await loop.wait_idle()
    clock.now += 25
    await loop.handle_transcript(event("three", "still active?"))
    await loop.wait_idle()
    assert len(responder.calls) == 3
    await loop.close()


def test_activation_window_rejects_nonpositive_values():
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(FakeResponder(), activation_seconds=0)
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(FakeResponder(), activation_seconds=float("inf"))


async def test_barge_in_stops_playback_only_while_speaking():
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, barge_in=True)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "hello"))
    await loop.wait_idle()
    assert audio.stops == 0
    audio.speaking = True
    await loop.handle_transcript(event("two", "hold on"))
    await loop.wait_idle()
    assert audio.stops == 1
    assert audio.spoken[-1] == "hold on"
    await loop.close()


async def test_barge_in_disabled_never_stops_playback():
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    audio.speaking = True
    await loop.handle_transcript(event("one", "hello"))
    await loop.wait_idle()
    assert audio.stops == 0
    await loop.close()


async def test_agent_error_raises_typed_provider_fault():
    from fakeyou.observability.provider_faults import ProviderFault

    class FailingAgent:
        async def ainvoke(self, request, config):
            raise RuntimeError("agent exploded")

    responder = LangGraphResponder(FailingAgent(), "thread")
    with pytest.raises(ProviderFault) as caught:
        await responder.respond([event("one")], lambda: None)

    assert caught.value.source == "agent"
    assert caught.value.kind == "unknown"
    assert caught.value.__cause__ is not None


async def test_responder_error_still_fails_loop():
    class Broken:
        async def respond(self, events, on_tool_start):
            raise RuntimeError("model exploded")

    loop = ConversationLoop(Broken())
    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    assert loop.phase == "failed"
    await loop.close()


async def test_loop_journals_turn_without_modifying_injected_responder(
    journal,
):
    import json
    from dataclasses import dataclass

    lines = journal.lines

    @dataclass(frozen=True)
    class JournaledResponder:
        journal: object | None = None

        async def respond(self, events, on_tool_start):
            return "ok"

    loop = ConversationLoop(JournaledResponder(), journal=journal)

    loop.bind(FakeReplyAudio())
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert loop._responder.journal is None
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
def test_voice_cap_uses_language_aware_sentence_boundaries(reply, expected):
    assert cap_spoken_reply(reply) == expected


def _recall_then_reply() -> ToolCallingFakeModel:
    return ToolCallingFakeModel(
        responses=[
            recall_call({"query": "demo"}, id="call-1"),
            AIMessage(content="The demo is on Friday."),
        ]
    )


async def test_responder_reports_tool_results_to_journal_and_callback(
    journal,
):
    import json

    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        return (
            "From owner note on 2026-09-30 06:40 UTC: The demo is on Friday\n"
            "From Google Meet m1 transcript on 2026-09-23 03:10 UTC: Budi is the PIC"
        )

    results: list[tuple] = []
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


async def test_responder_journals_tool_failures(
    journal,
):
    import json

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


async def test_interim_speech_interrupts_playback_after_enough_words(
    journal,
):
    import json

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), barge_in=True, journal=journal)
    loop.bind(audio)

    await loop.handle_interim("okay")
    await loop.handle_interim("okay wait")
    assert audio.stops == 0
    await loop.handle_interim("okay wait please")

    assert audio.stops == 1
    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert (row["trigger"], row["words"]) == ("interim", 3)


async def test_interim_speech_is_ignored_without_barge_in_or_playback():
    audio = FakeReplyAudio()
    audio.speaking = True
    quiet = ConversationLoop(FakeResponder())
    quiet.bind(audio)
    await quiet.handle_interim("please stop for now")

    idle_audio = FakeReplyAudio()
    idle = ConversationLoop(FakeResponder(), barge_in=True)
    idle.bind(idle_audio)
    await idle.handle_interim("please stop for now")

    assert audio.stops == 0
    assert idle_audio.stops == 0


async def test_interim_barge_in_respects_wake_phrases():
    clock = FakeClock()
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), barge_in=True, wake_phrases=["rani twin"], clock=clock)
    loop.bind(audio)

    await loop.handle_interim("someone else is talking now")
    assert audio.stops == 0
    await loop.handle_transcript(event("t1", "rani twin please explain"))
    audio.stops = 0
    audio.speaking = True
    await loop.handle_interim("wait just a moment")

    assert audio.stops == 1
    await loop.close()


async def test_final_barge_in_is_journaled_with_its_trigger(
    journal,
):
    import json

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), barge_in=True, journal=journal)
    loop.bind(audio)

    await loop.handle_transcript(event("t1", "enough"))

    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert row["trigger"] == "final"
    await loop.close()


async def test_one_interruption_is_journaled_and_stopped_once(
    journal,
):

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), barge_in=True, journal=journal)
    loop.bind(audio)

    await loop.handle_interim("wait a second")
    await loop.handle_interim("wait a second please")

    assert audio.stops == 1
    assert len([line for line in lines if '"barge_in_detected"' in line]) == 1


async def test_interim_barge_in_rearms_for_the_next_reply():
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), barge_in=True)
    loop.bind(audio)

    await loop.handle_interim("wait a second")
    await loop.handle_transcript(event("t1", "what is next"))
    await loop.wait_idle()
    audio.speaking = True
    audio.stops = 0
    await loop.handle_interim("hold on one moment")

    assert audio.spoken == ["what is next"]
    assert audio.stops == 1
    await loop.close()


async def test_model_and_tool_events_carry_their_transcript(journal):
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


async def test_failed_model_request_keeps_transcript_and_error_type(journal):
    from fakeyou.observability.provider_faults import ProviderFault

    class FailingChatModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
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


async def test_cancelled_tool_run_keeps_transcript_without_content(journal):
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


class ScriptedStream:
    def __init__(self, owner, actions, callbacks):
        self._owner = owner
        self._actions = list(actions)
        self._callbacks = list(callbacks)
        self.delivered = 0
        self.aclosed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._actions:
            action = self._actions.pop(0)
            if action[0] == "chunk":
                self.delivered += 1
                self._owner.delivered += 1
                return AIMessageChunk(content=action[1]), {"langgraph_node": action[2]}
            if action[0] == "tool":
                run_id = uuid.uuid4()
                for callback in self._callbacks:
                    await callback.on_tool_start({"name": action[1]}, "", run_id=run_id)
                if isinstance(action[2], Exception):
                    for callback in self._callbacks:
                        await callback.on_tool_error(action[2], run_id=run_id)
                else:
                    outcome = ToolMessage(content=action[2], tool_call_id="call-1")
                    for callback in self._callbacks:
                        await callback.on_tool_end(outcome, run_id=run_id)
                continue
            if action[0] == "gate":
                action[1].set()
                await action[2].wait()
                continue
            if action[0] == "error":
                raise action[1]
            raise AssertionError(action[0])
        raise StopAsyncIteration

    async def aclose(self):
        self.aclosed = True
        self._owner.closed += 1


class ScriptedAgent:
    def __init__(self, scripts, fallback_reply=""):
        self._scripts = scripts
        self.fallback_reply = fallback_reply
        self.streams = []
        self.configs = []
        self.stream_modes = []
        self.delivered = 0
        self.closed = 0
        self.ainvoke_calls = []

    def astream(self, payload, config=None, **kwargs):
        key = payload["messages"][-1].id
        if isinstance(self._scripts, dict):
            actions = self._scripts.get(key, self._scripts.get("*", []))
        else:
            actions = self._scripts
        self.configs.append(config)
        self.stream_modes.append(kwargs.get("stream_mode"))
        stream = ScriptedStream(self, actions, (config or {}).get("callbacks", ()))
        self.streams.append(stream)
        return stream

    async def ainvoke(self, payload, config=None, **kwargs):
        self.ainvoke_calls.append(payload)
        return {"messages": [AIMessage(content=self.fallback_reply)]}


def streamed_loop(agent, audio, journal, language="id", **kwargs):
    responder = StreamingLangGraphResponder(agent, "thread", journal=journal, language=language)
    loop = ConversationLoop(
        responder, sentence_streaming=True, language=language, journal=journal, **kwargs
    )
    loop.bind(audio)
    return loop


async def test_streamed_reply_speaks_each_sentence_in_order(journal):
    agent = ScriptedAgent(
        [
            ("chunk", "First sent", "model"),
            ("chunk", "ence here. Second sent", "model"),
            ("chunk", "ence here.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert " ".join(audio.spoken) == cap_spoken_reply("First sentence here. Second sentence here.")
    [started] = journal.events("agent_started")
    [first] = journal.events("answer_text_emitted")
    assert first["source"] == "llm"
    assert (first["request_id"], first["transcript_id"]) == (started["request_id"], "t1")
    emitted = journal.events("answer_sentence_emitted")
    assert [(row["sentence_index"], row["chars"]) for row in emitted] == [(0, 20), (1, 21)]
    assert {row["request_id"] for row in emitted} == {started["request_id"]}
    assert agent.stream_modes == ["messages"]
    assert journal.events("agent_finished") != []
    await loop.close()


@pytest.mark.parametrize("language", ["id", "en"])
async def test_streamed_reply_matches_cap_across_languages(journal, language):
    agent = ScriptedAgent(
        [("chunk", "Morning update. All ", "model"), ("chunk", "systems ready.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, language=language)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Morning update.", "All systems ready."]
    assert " ".join(audio.spoken) == cap_spoken_reply(
        "Morning update. All systems ready.", language=language
    )
    await loop.close()


async def test_streamed_reply_skips_tool_text_and_empty_chunks(journal):
    agent = ScriptedAgent(
        [
            ("chunk", "", "model"),
            ("chunk", "tool output secret", "tools"),
            ("chunk", "Real answer here. Done now.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Real answer here.", "Done now."]
    body = "\n".join(journal.lines)
    assert "tool output secret" not in body
    assert "Real answer here" not in body
    await loop.close()


async def test_streaming_responder_with_flag_off_uses_single_speak(journal):
    agent = ScriptedAgent([], fallback_reply="One. Two. Three.")
    audio = FakeReplyAudio()
    responder = StreamingLangGraphResponder(agent, "thread", journal=journal)
    loop = ConversationLoop(responder, journal=journal)
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One. Two."]
    assert journal.events("answer_text_emitted") == []
    assert journal.events("answer_sentence_emitted") == []
    assert len(agent.ainvoke_calls) == 1
    assert agent.streams == []
    await loop.close()


async def test_flag_on_with_plain_responder_keeps_old_behavior():
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, sentence_streaming=True)
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["hello"]
    await loop.close()


async def test_streamed_reply_discards_partial_text_before_tool_end(journal):
    agent = ScriptedAgent(
        [
            ("chunk", "Checking memor", "model"),
            ("tool", "recall_meeting_context", "demo friday"),
            ("chunk", "The demo is on ", "model"),
            ("chunk", "Friday. Doors open at nine.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["The demo is on Friday.", "Doors open at nine."]
    body = "\n".join(journal.lines)
    assert "Checking memor" not in body
    assert "demo friday" not in body
    [started_tool] = journal.events("tool_started")
    assert (started_tool["tool"], started_tool["transcript_id"]) == ("recall_meeting_context", "t1")
    [finished_tool] = journal.events("tool_finished")
    assert finished_tool["transcript_id"] == "t1"
    await loop.close()


async def test_streamed_reply_marks_answer_text_after_tool_end(journal):
    import json

    agent = ScriptedAgent(
        [
            ("chunk", "", "model"),
            ("tool", "recall_meeting_context", "demo friday"),
            ("chunk", "The demo is on Friday.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["The demo is on Friday."]
    order = [json.loads(line)["event"] for line in journal.lines]
    assert order.index("answer_text_emitted") > order.index("tool_finished")
    await loop.close()


async def test_streamed_reply_survives_recall_failure(journal):
    from langchain_core.tools import tool

    @tool(description="Search long-term meeting memory for relevant context.")
    async def recall_meeting_context(query: str) -> str:
        raise RuntimeError("graph down")

    agent = create_meeting_agent(
        ToolCallingFakeModel(
            responses=[
                recall_call({"query": "demo"}, id="call-1"),
                AIMessage(content="I do not know the answer. Sorry about that."),
            ]
        ),
        tools=[recall_meeting_context],
        checkpointer=InMemorySaver(),
        middleware=[create_recall_error_middleware(None)],
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "when is demo"))
    await loop.wait_idle()
    assert audio.spoken == ["I do not know the answer.", "Sorry about that."]
    assert "Meeting memory is unavailable" not in " ".join(audio.spoken)
    [failed] = journal.events("tool_failed")
    assert (failed["tool"], failed["transcript_id"]) == ("recall_meeting_context", "t1")
    assert journal.events("agent_finished") != []
    await loop.close()


async def test_streamed_reply_uses_fallback_model(journal):
    from langchain.agents.middleware import ModelFallbackMiddleware

    class FailingChatModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            raise RuntimeError("primary down")

    agent = create_meeting_agent(
        FailingChatModel(responses=[AIMessage(content="unused")]),
        checkpointer=InMemorySaver(),
        middleware=[
            ModelFallbackMiddleware(
                ToolCallingFakeModel(responses=[AIMessage(content="Fallback answer here.")])
            )
        ],
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Fallback answer here."]
    assert journal.events("agent_finished") != []
    await loop.close()


async def test_streamed_reply_caps_third_sentence_and_stops_stream(journal):
    agent = ScriptedAgent(
        [("chunk", "One. Two. Three.", "model"), ("chunk", " Four. Five.", "model")]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert " ".join(audio.spoken) == cap_spoken_reply("One. Two. Three. Four. Five.")
    [capped] = journal.events("reply_capped")
    assert capped["chars_in"] >= capped["chars_out"] > 0
    assert agent.delivered == 1
    assert agent.streams[0].aclosed
    await loop.close()


async def test_streamed_reply_without_remainder_records_no_cap(journal):
    agent = ScriptedAgent([("chunk", "One. Two.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["One.", "Two."]
    assert journal.events("reply_capped") == []
    await loop.close()


async def test_streamed_empty_reply_moves_to_waiting(journal):
    agent = ScriptedAgent([])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == []
    assert loop.phase == "waiting"
    await loop.close()


def test_streaming_responder_rejects_non_positive_cap():
    with pytest.raises(ValueError, match="max_sentences"):
        StreamingLangGraphResponder(ScriptedAgent([]), "thread", max_sentences=0)


async def test_new_transcript_mid_stream_drops_remainder(journal):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        {
            "t1": [
                ("chunk", "First done. ", "model"),
                ("gate", ready, release),
                ("chunk", "Second part here. ", "model"),
            ],
            "t2": [("chunk", "New answer.", "model")],
        }
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.handle_transcript(event("t2", "second"))
    release.set()
    await loop.wait_idle()
    assert audio.spoken == ["First done.", "New answer."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["request_id"], dropped["transcript_id"]) == ("t1:1", "t1")
    assert (dropped["sentence_index"], dropped["detail"]) == (1, "stale_generation")
    assert agent.streams[0].aclosed
    await loop.close()


async def test_interim_stop_between_sentences_speaks_nothing_further(journal):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part here. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, barge_in=True)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.handle_interim("wait just a moment")
    assert audio.stops == 1
    release.set()
    await loop.wait_idle()
    assert audio.spoken == ["First done."]
    [dropped] = journal.events("sentence_dropped")
    assert (dropped["transcript_id"], dropped["sentence_index"], dropped["detail"]) == (
        "t1",
        1,
        "stopped",
    )
    await loop.close()


async def test_close_during_stream_cancels_without_trailing_speech(journal):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.close()
    assert audio.spoken == ["First done."]
    assert agent.streams[0].aclosed
    assert journal.events("agent_cancelled") != []


async def test_streamed_journal_events_carry_metadata_only(journal):
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    [first] = journal.events("answer_text_emitted")
    assert set(first) == {"event", "meeting_id", "at", "source", "request_id", "transcript_id"}
    emitted = journal.events("answer_sentence_emitted")
    assert [set(row) for row in emitted] == [
        {
            "event",
            "meeting_id",
            "at",
            "source",
            "request_id",
            "transcript_id",
            "sentence_index",
            "chars",
        }
    ] * 2
    body = "\n".join(journal.lines)
    assert "First sentence here" not in body
    assert "Second sentence here" not in body
    assert "hello" not in body
    await loop.close()


async def test_stream_failure_before_first_sentence_falls_back_to_full_reply(journal):
    agent = ScriptedAgent(
        [("error", RuntimeError("stream unsupported"))],
        fallback_reply="Full reply one. Full reply two. Full reply three.",
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["Full reply one.", "Full reply two."]
    [failed] = [row for row in journal.events("agent_failed") if row.get("detail") == "stream"]
    assert failed["request_id"] == "t1:1"
    assert journal.events("agent_finished") != []
    assert len(agent.ainvoke_calls) == 1
    await loop.close()


async def test_stream_failure_after_first_sentence_propagates_without_duplicate(journal):
    agent = ScriptedAgent(
        [("chunk", "First done. ", "model"), ("error", RuntimeError("mid stream"))]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    assert audio.spoken == ["First done."]
    assert journal.events("agent_finished") == []
    assert agent.ainvoke_calls == []
    await loop.close()


async def test_streamed_speak_tags_each_sentence_with_its_index(journal):
    agent = ScriptedAgent([("chunk", "First sentence here. Second sentence here.", "model")])
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.indices == [0, 1]
    await loop.close()


async def test_stale_drop_terminates_turn_with_agent_cancelled(journal):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        {
            "t1": [
                ("chunk", "First done. ", "model"),
                ("gate", ready, release),
                ("chunk", "Second part here. ", "model"),
            ],
            "t2": [("chunk", "New answer.", "model")],
        }
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    await loop.handle_transcript(event("t2", "second"))
    release.set()
    await loop.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["request_id"] == "t1:1"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    assert "duration_ms" in cancelled[0]
    finished = [row for row in journal.events("agent_finished") if row["request_id"] == "t1:1"]
    assert finished == []
    await loop.close()


async def test_stopped_drop_terminates_turn_with_agent_cancelled(journal):
    ready = asyncio.Event()
    release = asyncio.Event()
    agent = ScriptedAgent(
        [
            ("chunk", "First done. ", "model"),
            ("gate", ready, release),
            ("chunk", "Second part here. ", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal, barge_in=True)
    await loop.handle_transcript(event("t1", "first"))
    assert await asyncio.wait_for(ready.wait(), 10)
    audio.speaking = True
    await loop.handle_interim("wait just a moment")
    release.set()
    await loop.wait_idle()
    [dropped] = journal.events("sentence_dropped")
    assert dropped["detail"] == "stopped"
    cancelled = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert len(cancelled) == 1
    await loop.close()


def test_sentence_streaming_default_follows_settings_flag():
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.sentence_streaming is False
    params = inspect.signature(ConversationLoop).parameters
    assert params["sentence_streaming"].default == DEFAULTS.conversation.sentence_streaming


async def test_streamed_speak_passes_transcript_id_to_session(journal):
    agent = ScriptedAgent(
        [
            ("chunk", "First sent", "model"),
            ("chunk", "ence here. Second sent", "model"),
            ("chunk", "ence here.", "model"),
        ]
    )
    audio = FakeReplyAudio()
    loop = streamed_loop(agent, audio, journal)
    await loop.handle_transcript(event("t1", "hello"))
    await loop.wait_idle()
    assert audio.spoken == ["First sentence here.", "Second sentence here."]
    assert audio.transcripts == ["t1", "t1"]
    await loop.close()


class GatedAudio(FakeReplyAudio):
    def __init__(self, gate):
        super().__init__()
        self.gate = gate
        self.entered = asyncio.Event()
        self.attempts = 0

    async def speak(self, text, *, sentence_index=None, transcript_id=None):
        self.attempts += 1
        self.entered.set()
        await self.gate.wait()
        await super().speak(text, sentence_index=sentence_index, transcript_id=transcript_id)


async def test_interim_cancels_pre_pcm_child_without_answering(journal):
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, barge_in=True, journal=journal)
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


async def test_duplicate_final_and_interim_do_not_reschedule(journal):
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, barge_in=True, journal=journal)
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


async def test_close_cancels_active_turn_and_never_reschedules(journal):
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
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


async def test_new_final_during_tts_drops_stale_audio(journal):
    gate = asyncio.Event()
    responder, audio = FakeResponder(), GatedAudio(gate)
    loop = ConversationLoop(responder, journal=journal)
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


async def test_close_during_tts_speak_drops_audio_and_stays_closed(journal):
    gate = asyncio.Event()
    responder, audio = FakeResponder(), GatedAudio(gate)
    loop = ConversationLoop(responder, journal=journal)
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


async def test_cancelled_request_ids_do_not_attach_to_next_transcript(journal):
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
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


async def test_cancel_between_tool_call_and_result_keeps_usable_checkpoint(journal):
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

    def named_call(name, call_id):
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
    loop = ConversationLoop(responder, journal=journal)
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


async def test_cancelled_model_request_does_not_use_fallback(journal):
    from langchain.agents.middleware import ModelFallbackMiddleware

    entered = asyncio.Event()
    attempts: list[int] = []
    fallback_calls: list[int] = []

    class HangingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                entered.set()
                await asyncio.Event().wait()
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    class CountingModel(ToolCallingFakeModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            fallback_calls.append(1)
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    agent = create_meeting_agent(
        HangingModel(responses=[AIMessage(content="primary answer")]),
        checkpointer=InMemorySaver(),
        middleware=[ModelFallbackMiddleware(CountingModel(responses=[AIMessage(content="x")]))],
    )
    responder = LangGraphResponder(agent, "fallback-cancel-thread", journal=journal)
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
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


async def test_local_transport_cancel_closes_loopback_without_fallback(journal):
    from langchain_openai import ChatOpenAI

    arrived = asyncio.Event()
    disconnected = asyncio.Event()
    connections = 0

    async def hold(reader, writer):
        nonlocal connections
        connections += 1
        try:
            await reader.readuntil(b"\r\n\r\n")
            arrived.set()
            await reader.read()
            disconnected.set()
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionResetError):
            arrived.set()
        finally:
            writer.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    sockets = server.sockets or []
    port = sockets[0].getsockname()[1]
    model = ChatOpenAI(
        model="loopback-dummy",
        api_key="test-key",
        base_url=f"http://127.0.0.1:{port}/v1",
        request_timeout=30,
        max_retries=0,
    )
    agent = create_meeting_agent(model, checkpointer=InMemorySaver())
    loop = ConversationLoop(
        LangGraphResponder(agent, "loopback-thread", journal=journal), journal=journal
    )
    audio = FakeReplyAudio()
    loop.bind(audio)
    try:
        await loop.handle_transcript(event("t1", "hello"))
        assert await asyncio.wait_for(arrived.wait(), 2)
        assert connections == 1
        await loop.close()
        assert await asyncio.wait_for(disconnected.wait(), 2)
        assert connections == 1
        assert audio.spoken == []
        assert journal.events("agent_failed") == []
        [requested] = journal.events("agent_cancel_requested")
        assert requested["reason"] == "close"
        [cancelled] = journal.events("agent_cancelled")
        assert cancelled["reason"] == "close"
    finally:
        server.close()
        await server.wait_closed()


def test_policy_requires_configured_aliases():
    responder = FakeResponder()
    with pytest.raises(ValueError, match="twin aliases"):
        ConversationLoop(responder, participant_policy_enabled=True)
    with pytest.raises(ValueError, match="twin aliases"):
        ConversationLoop(responder, wake_phrases=["  "], participant_policy_enabled=True)


async def test_policy_silent_makes_zero_responder_calls(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, wake_phrases=["Rani Twin"], participant_policy_enabled=True
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Budi, when is the demo?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert responder.calls == []
    assert audio.spoken == []
    [decision] = journal.events("policy_decided")
    assert (decision["transcript_id"], decision["action"], decision["reason"]) == (
        "t1",
        "silent",
        "not_addressed",
    )
    await loop.close()


async def test_policy_keeps_barge_in_on_speech_onset(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        wake_phrases=["Rani Twin"],
        participant_policy_enabled=True,
        barge_in=True,
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t0", "Rani Twin, when is the demo?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    audio.speaking = True
    await loop.handle_interim("wait just a moment please")
    assert audio.stops == 1
    await loop.handle_transcript(event("t1", "Budi, when is the demo?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    await loop.close()


async def test_policy_answers_vocative_and_ignores_quote_mention(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, wake_phrases=["Rani Twin"], participant_policy_enabled=True
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani Twin, when is the demo?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    await loop.handle_transcript(
        event("t2", "He said Rani Twin agrees", participant_id="spaces/x/devices/9")
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    assert audio.spoken == ["Rani Twin, when is the demo?"]
    actions = [row["action"] for row in journal.events("policy_decided")]
    assert actions == ["reply", "silent"]
    await loop.close()


async def test_policy_unknown_followup_does_not_inherit_activation(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        journal=journal,
        wake_phrases=["Rani Twin"],
        participant_policy_enabled=True,
        clock=clock.monotonic,
    )
    loop.bind(audio)
    await loop.handle_transcript(
        event(
            "t1",
            "Rani Twin, when is the demo?",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e1",
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(event("t2", "What time exactly?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    reasons = [row["reason"] for row in journal.events("policy_decided")]
    assert reasons == ["addressed", "not_addressed"]
    await loop.close()


async def test_policy_unknown_followup_after_unknown_vocative_stays_silent(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        journal=journal,
        wake_phrases=["Rani Twin"],
        participant_policy_enabled=True,
        clock=clock.monotonic,
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani Twin, when is the demo?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(event("t2", "What time exactly?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    reasons = [row["reason"] for row in journal.events("policy_decided")]
    assert reasons == ["addressed", "not_addressed"]
    await loop.close()


async def test_policy_other_known_speaker_followup_stays_silent(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        journal=journal,
        wake_phrases=["Rani Twin"],
        participant_policy_enabled=True,
        clock=clock.monotonic,
    )
    loop.bind(audio)
    await loop.handle_transcript(
        event(
            "t1",
            "Rani Twin, when is the demo?",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e1",
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(
        event(
            "t2",
            "Move it to Thursday?",
            participant_id="spaces/x/devices/2",
            speaker_evidence_id="e2",
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    reasons = [row["reason"] for row in journal.events("policy_decided")]
    assert reasons == ["addressed", "not_addressed"]
    await loop.close()


async def test_policy_same_speaker_followup_replies(journal):
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        journal=journal,
        wake_phrases=["Rani Twin"],
        participant_policy_enabled=True,
        clock=clock.monotonic,
    )
    loop.bind(audio)
    await loop.handle_transcript(
        event(
            "t1",
            "Rani Twin, when is the demo?",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e1",
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(
        event(
            "t2",
            "What time exactly?",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e2",
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == [
        "Rani Twin, when is the demo?",
        "What time exactly?",
    ]
    reasons = [row["reason"] for row in journal.events("policy_decided")]
    assert reasons == ["addressed", "followup_same_speaker"]
    await loop.close()


def test_request_messages_carry_speaker_identity_without_owner_data():
    addressed = TranscriptEvent(
        "t1",
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        "Rani Twin, hello",
        True,
        participant_id="spaces/x/devices/1",
        speaker_evidence_id="e1",
    )
    (known,) = _request_messages([addressed])
    assert known.additional_kwargs["meeting_event"] == {
        "timestamp": addressed.timestamp,
        "source": "meeting_audio",
        "participant_id": "spaces/x/devices/1",
        "speaker_evidence_id": "e1",
        "stream_id": "",
        "speaker_status": "observed",
    }
    (anonymous,) = _request_messages([event("t2", "Hello")])
    assert anonymous.additional_kwargs["meeting_event"]["speaker_status"] == "unknown"


ACK_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "ack-cases.json").read_text(encoding="utf-8")
)


def test_bare_ack_vocabulary_is_pinned() -> None:
    assert BARE_ACK_VOCABULARY == frozenset(ACK_CASES["vocabulary"])


@pytest.mark.parametrize("text", ACK_CASES["observed_bare"] + ACK_CASES["isolated"])
def test_observed_single_token_acks_match_exactly(text: str) -> None:
    assert _is_bare_ack(text)


@pytest.mark.parametrize("text", ACK_CASES["replies"] + ACK_CASES["blanks"])
def test_substantive_text_is_never_a_bare_ack(text: str) -> None:
    assert not _is_bare_ack(text)


@pytest.mark.parametrize("text", ACK_CASES["isolated"])
async def test_isolated_bare_ack_still_gets_a_reply(text: str) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("t1", text))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [[text]]
    assert audio.spoken == [text]
    await loop.close()


async def test_bare_ack_run_suppresses_after_first(journal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
    loop.bind(audio)
    first, second, third = ACK_CASES["run"]
    await loop.handle_transcript(event("t1", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", second))
    await loop.wait_idle()
    await loop.handle_transcript(event("t3", third))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [[first]]
    assert audio.spoken == [first]
    assert [item.text for item in loop.events] == [first, second, third]
    await loop.close()


async def test_ack_suppression_journals_a_silent_decision(journal) -> None:
    loop = ConversationLoop(FakeResponder(), journal=journal)
    loop.bind(FakeReplyAudio())
    first, second = ACK_CASES["run"][0], ACK_CASES["run"][1]
    await loop.handle_transcript(event("t1", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", second))
    await loop.wait_idle()
    [decided] = journal.events("ack_decided")
    assert (decided["transcript_id"], decided["action"], decided["reason"]) == (
        "t2",
        "silent",
        "bare_ack_run",
    )
    await loop.close()


@pytest.mark.parametrize("text", ACK_CASES["replies"])
async def test_substantive_text_replies_inside_ack_runs(journal, text: str) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
    loop.bind(audio)
    first = ACK_CASES["run"][0]
    await loop.handle_transcript(event("t1", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", text))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [[first], [text]]
    assert audio.spoken == [first, text]
    assert journal.events("ack_decided") == []
    await loop.close()


async def test_substantive_text_breaks_the_ack_run(journal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
    loop.bind(audio)
    first, second, fourth = ACK_CASES["run"]
    third = ACK_CASES["run_breaker"]
    await loop.handle_transcript(event("t1", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", second))
    await loop.wait_idle()
    await loop.handle_transcript(event("t3", third))
    await loop.wait_idle()
    await loop.handle_transcript(event("t4", fourth))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [
        [first],
        [third],
        [fourth],
    ]
    assert audio.spoken == [first, third, fourth]
    [decided] = journal.events("ack_decided")
    assert decided["transcript_id"] == "t2"
    await loop.close()


async def test_vocative_text_replies_inside_ack_runs(journal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal)
    loop.bind(audio)
    first = ACK_CASES["run"][0]
    addressed = ACK_CASES["vocative"]
    await loop.handle_transcript(event("t1", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", addressed))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [[first], [addressed]]
    assert audio.spoken == [first, addressed]
    assert journal.events("ack_decided") == []
    await loop.close()


async def test_addressed_text_replies_inside_wake_window_ack_runs(journal) -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder, wake_phrases=["rani twin"], activation_seconds=30, clock=clock, journal=journal
    )
    loop.bind(audio)
    first, second = ACK_CASES["run"][0], ACK_CASES["run"][1]
    await loop.handle_transcript(event("t1", "rani twin hello"))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", first))
    await loop.wait_idle()
    await loop.handle_transcript(event("t3", second))
    await loop.wait_idle()
    await loop.handle_transcript(event("t4", "rani twin, continue now"))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [
        ["rani twin hello"],
        [first],
        ["rani twin, continue now"],
    ]
    [decided] = journal.events("ack_decided")
    assert decided["transcript_id"] == "t3"
    await loop.close()


def test_transcript_event_and_record_carry_capture_ms(tmp_path: Path) -> None:
    from fakeyou.persistence.record import MeetingRecord, TranscriptEntry, load_meeting_record

    event = transcript_event(
        TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00", finalized=True),
        "a",
        None,
        "s0",
        capture_ms=55.5,
    )
    assert event.capture_ms == 55.5
    assert (
        transcript_event(
            TranscriptionFrame("hi", "", "2026-10-06T01:00:01+00:00", finalized=True),
            "a",
            None,
            "s0",
        ).capture_ms
        == 0.0
    )
    record = MeetingRecord(tmp_path / "record.jsonl")
    record.add_transcript(event)
    record.close()
    [entry] = load_meeting_record(tmp_path / "record.jsonl")
    assert isinstance(entry, TranscriptEntry)
    assert entry.capture_ms == 55.5


class _StreamingResponder:
    def __init__(self, sentences: list[str]) -> None:
        self.sentences = sentences
        self.calls: list[list[TranscriptEvent]] = []
        self.released = asyncio.Event()

    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self):
        for sentence in self.sentences:
            yield sentence


def journal_has(journal, name, **fields):
    return any(
        all(row.get(key) == value for key, value in fields.items()) for row in journal.events(name)
    )


async def test_pregeneration_adopts_exact_match_without_second_call(journal) -> None:
    responder = _StreamingResponder(["Yes I can.", "Let me check."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can.", "Let me check."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


async def test_pregeneration_discards_on_mismatch_and_uses_normal_path(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded")
    await loop.close()


async def test_pregeneration_stays_off_by_default(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal, sentence_streaming=True)
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.close()


async def test_note_vad_fires_pregen_and_adopts_exact_final(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_started")
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    assert audio.spoken == ["Yes I can."]
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.close()


async def test_note_vad_stays_silent_on_speech_start(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(True)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_without_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_fires_on_short_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("see you")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    await loop.close()


async def test_note_vad_stays_silent_on_empty_interim(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("  ")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_when_disabled(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(responder, journal=journal, sentence_streaming=True)
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_note_vad_stays_silent_when_busy(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_post_stop_interim_starts_pregen_with_single_word(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert responder.calls == []
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal_has(journal, "pregeneration_started")
    await loop.close()


async def test_post_stop_interim_restarts_pregen_on_changed_words(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert len(journal.events("pregeneration_started")) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    await loop.close()


async def test_post_stop_interim_keeps_pregen_on_identical_words(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    assert journal.events("pregeneration_finished") == []
    await loop.close()


async def test_mid_speech_interim_starts_no_post_stop_pregen(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.note_vad(True)
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_vad_start_clears_post_stop_pregen_flag(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.note_vad(True)
    await loop.note_vad(False)
    await loop.note_vad(True)
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert responder.calls == []
    assert journal.events("pregeneration_started") == []
    await loop.close()


async def test_final_clears_post_stop_pregen_flag(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.note_vad(False)
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.handle_interim("hello")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert journal.events("pregeneration_started") == []
    await loop.close()


_DIFF_KEYS = ("dist", "words_pre", "words_final", "norm_dist_bucket", "diff_class")


async def test_pregen_superseded_journal_carries_word_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "superseded"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me"), normalize_words("can you help me today")
    )
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_mismatched_journal_carries_word_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "cancel everything now", finalized=True))
    await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "mismatched"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me today?"), normalize_words("cancel everything now")
    )
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    await loop.close()


async def test_pregen_adopted_journal_carries_exact_diff(journal) -> None:
    responder = _StreamingResponder(["Yes I can.", "Let me check."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("outcome") == "adopted"
    ]
    assert row["dist"] == 0
    assert row["diff_class"] == "exact"
    assert row["words_pre"] == row["words_final"] == len(normalize_words("can you help me today?"))
    await loop.close()


class _FailingStreamResponder:
    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        raise RuntimeError("stream failed")


async def test_pregen_error_discard_journal_carries_no_diff(journal) -> None:
    responder = _FailingStreamResponder()
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me today?")
    await loop.handle_interim("can you help me today?")
    await asyncio.sleep(0.05)
    assert len(journal.events("pregeneration_started")) == 1
    await loop.handle_transcript(event("f1", "can you help me today?", finalized=True))
    with pytest.raises(ConversationError):
        await loop.wait_idle()
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "error"
    ]
    for key in _DIFF_KEYS:
        assert key not in row
    await loop.close()


async def test_pregen_journal_diff_values_are_metadata_only(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    await loop.handle_transcript(event("f1", "can you help me today", finalized=True))
    await loop.wait_idle()
    rows = journal.events("pregeneration_finished")
    assert len(rows) >= 2
    allowed = {"exact", "prefix_extend", "prefix_truncate", "single_subst", "other"}
    for row in rows:
        for key in ("dist", "words_pre", "words_final"):
            if key in row:
                assert isinstance(row[key], int) and not isinstance(row[key], bool)
        if "norm_dist_bucket" in row:
            assert isinstance(row["norm_dist_bucket"], float)
        if "diff_class" in row:
            assert row["diff_class"] in allowed
        for key in _DIFF_KEYS:
            if key in row:
                assert "can you help" not in str(row[key])
    await loop.close()


class _GatedStreamingResponder:
    def __init__(self) -> None:
        self.calls: list[list[TranscriptEvent]] = []
        self.release = asyncio.Event()

    def stream_sentences(self, events, on_tool_start, *, request_id: str):
        self.calls.append(list(events))
        return self._generate()

    async def _generate(self):
        await self.release.wait()
        yield "Yes I can."


async def test_pregen_fuzzy_restart_suppresses_suffix_extension(journal) -> None:
    responder = _GatedStreamingResponder()
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        sentence_streaming=True,
        pre_generate_enabled=True,
        pregen_fuzzy_restart_enabled=True,
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    before = loop._pregen
    assert before is not None
    request_id = before.request_id
    task = before.task
    assert task is not None and not task.done()
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    current = loop._pregen
    assert current is not None
    assert current.request_id == request_id
    assert current.task is task
    assert not task.done()
    assert current.words == normalize_words("can you help me today")
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == request_id
    assert row["diff_class"] == "prefix_extend"
    for key in _DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    responder.release.set()
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_single_substitution(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        sentence_streaming=True,
        pre_generate_enabled=True,
        pregen_fuzzy_restart_enabled=True,
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert loop._pregen is not None
    before_id = loop._pregen.request_id
    await loop.handle_interim("can you help him")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert loop._pregen is not None
    assert loop._pregen.request_id != before_id
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_restarts_on_small_non_suffix_drift(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        sentence_streaming=True,
        pre_generate_enabled=True,
        pregen_fuzzy_restart_enabled=True,
    )
    loop.bind(audio)
    await loop.handle_interim("see you")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("see them")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_suppresses_small_drift_on_long_utterance(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        sentence_streaming=True,
        pre_generate_enabled=True,
        pregen_fuzzy_restart_enabled=True,
    )
    loop.bind(audio)
    await loop.handle_interim("please can you help me today")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert loop._pregen is not None
    request_id = loop._pregen.request_id
    await loop.handle_interim("please can you help me tonight")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    assert len(journal.events("pregeneration_started")) == 1
    current = loop._pregen
    assert current is not None
    assert current.request_id == request_id
    assert current.words == normalize_words("please can you help me tonight")
    [row] = journal.events("pregeneration_restart_skipped")
    assert row["request_id"] == request_id
    assert row["dist"] == 1
    for key in _DIFF_KEYS:
        assert key in row
    assert journal.events("pregeneration_finished") == []
    await loop.close()


async def test_pregen_restart_without_fuzzy_flag_restarts_on_suffix_extension(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder, journal=journal, sentence_streaming=True, pre_generate_enabled=True
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 2
    assert journal_has(journal, "pregeneration_finished", outcome="discarded", reason="superseded")
    assert journal.events("pregeneration_restart_skipped") == []
    await loop.close()


async def test_pregen_fuzzy_restart_suppressed_pregen_rejects_inexact_final(journal) -> None:
    responder = _StreamingResponder(["Yes I can."])
    audio = FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        journal=journal,
        sentence_streaming=True,
        pre_generate_enabled=True,
        pregen_fuzzy_restart_enabled=True,
    )
    loop.bind(audio)
    await loop.handle_interim("can you help me")
    await loop.note_vad(False)
    await asyncio.sleep(0.05)
    assert len(responder.calls) == 1
    await loop.handle_interim("can you help me today")
    await asyncio.sleep(0.05)
    assert len(journal.events("pregeneration_restart_skipped")) == 1
    await loop.handle_transcript(event("f1", "something else entirely", finalized=True))
    await loop.wait_idle()
    assert len(responder.calls) == 2
    [row] = [
        row for row in journal.events("pregeneration_finished") if row.get("reason") == "mismatched"
    ]
    expected = describe_word_diff(
        normalize_words("can you help me today"), normalize_words("something else entirely")
    )
    for key in _DIFF_KEYS:
        assert row[key] == expected[key]
    assert not journal_has(journal, "pregeneration_finished", outcome="adopted")
    await loop.close()


def test_pregen_fuzzy_restart_default_follows_settings_flag():
    import inspect

    from fakeyou.config.settings import DEFAULTS

    assert DEFAULTS.conversation.pregen_fuzzy_restart_enabled is False
    params = inspect.signature(ConversationLoop).parameters
    assert (
        params["pregen_fuzzy_restart_enabled"].default
        == DEFAULTS.conversation.pregen_fuzzy_restart_enabled
    )
