import asyncio
from datetime import UTC, datetime

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.agent import create_meeting_agent
from fakeyou.conversation import (
    ConversationError,
    ConversationLoop,
    LangGraphResponder,
    TranscriptEvent,
    cap_spoken_reply,
    conversation_thread_id,
    transcript_event,
)
from tests.fakes import FakeClock, FakeReplyAudio, FakeResponder, ToolCallingFakeModel, recall_call


def event(id, text="hello", finalized=True):
    return TranscriptEvent(id, datetime.now(UTC).isoformat(), "meeting_audio", text, finalized)


async def test_serializes_turns_and_preserves_every_new_utterance():
    gate = asyncio.Event()
    responder, audio = FakeResponder(gate), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "first"))
    await responder.started.wait()
    await loop.handle_transcript(event("two", "second"))
    await loop.handle_transcript(event("three", "third"))
    assert len(responder.calls) == 1
    gate.set()
    await loop.wait_idle()
    assert responder.max_active == 1
    assert [[item.text for item in call] for call in responder.calls] == [
        ["first"],
        ["second", "third"],
    ]
    assert audio.spoken == ["second/third"]
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
    assert transcript_event(first).id == transcript_event(replay).id
    assert transcript_event(first).id != transcript_event(next_utterance).id
    assert transcript_event(first).participant == "unknown"


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
    from fakeyou.provider_faults import ProviderFault

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

    from fakeyou.provider_faults import ProviderFault

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
    from fakeyou.provider_faults import ProviderFault

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
