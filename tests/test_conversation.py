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


def event(id, text="hello", finalized=True):
    return TranscriptEvent(id, datetime.now(UTC).isoformat(), "meeting_audio", text, finalized)


class Responder:
    def __init__(self, gate=None):
        self.calls = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.gate = gate
        self.active = 0
        self.max_active = 0

    async def respond(self, events, on_tool_start):
        self.calls.append(events)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None and len(self.calls) == 1:
                await self.gate.wait()
            return "/".join(item.text for item in events)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        finally:
            self.active -= 1


class Audio:
    def __init__(self):
        self.spoken = []
        self.stops = 0
        self.speaking = False

    @property
    def is_speaking(self):
        return self.speaking

    async def speak(self, text):
        self.spoken.append(text)

    async def stop_playback(self):
        self.stops += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_serializes_turns_and_preserves_every_new_utterance():
    gate = asyncio.Event()
    responder, audio = Responder(gate), Audio()
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
    responder, audio = Responder(), Audio()
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
    responder, audio = Responder(asyncio.Event()), Audio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await responder.started.wait()
    await loop.close()
    await loop.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []


async def test_model_timeout_is_observable_without_exposing_provider_error():
    loop = ConversationLoop(Responder(asyncio.Event()), response_timeout_seconds=0.02)
    loop.bind(Audio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    await loop.close()


def test_cap_spoken_reply_keeps_short_replies() -> None:
    assert cap_spoken_reply("Satu kalimat.") == "Satu kalimat."
    assert cap_spoken_reply("Satu. Dua.") == "Satu. Dua."
    assert cap_spoken_reply("Tanpa titik akhir") == "Tanpa titik akhir"
    assert cap_spoken_reply("  ") == "  "


def test_cap_spoken_reply_cuts_after_two_sentences() -> None:
    assert cap_spoken_reply("Satu. Dua. Tiga. Empat. Lima.") == "Satu. Dua."
    assert cap_spoken_reply("Benarkah? Ya! Oke deh.") == "Benarkah? Ya!"


def test_cap_spoken_reply_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="max_sentences"):
        cap_spoken_reply("Satu.", 0)


async def test_loop_speaks_capped_reply_head() -> None:
    responder, audio = Responder(), Audio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "Satu. Dua. Tiga. Empat."))
    await loop.wait_idle()
    assert audio.spoken == ["Satu. Dua."]
    await loop.close()


async def test_observer_failure_preserves_model_and_speech():
    def broken(*args):
        raise RuntimeError("private-provider-data")

    responder, audio = Responder(), Audio()
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
    responder, audio, clock = Responder(), Audio(), Clock()
    loop = ConversationLoop(responder, wake_phrases=["hey fakeyou"], clock=clock)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "status proyek kami"))
    await loop.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    assert [item.text for item in loop.events] == ["status proyek kami"]
    await loop.close()


async def test_wake_phrase_activates_window_case_insensitively():
    responder, audio, clock = Responder(), Audio(), Clock()
    loop = ConversationLoop(
        responder, wake_phrases=["hey fakeyou"], activation_seconds=30, clock=clock
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "HEY Fakeyou, ada update?"))
    await loop.wait_idle()
    clock.now += 10
    await loop.handle_transcript(event("two", "lanjutkan"))
    await loop.wait_idle()
    clock.now += 31
    await loop.handle_transcript(event("three", "masih dengar?"))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [
        ["HEY Fakeyou, ada update?"],
        ["lanjutkan"],
    ]
    assert len(audio.spoken) == 2
    await loop.close()


async def test_wake_phrase_inside_window_extends_activation():
    responder, audio, clock = Responder(), Audio(), Clock()
    loop = ConversationLoop(
        responder, wake_phrases=["hey fakeyou"], activation_seconds=30, clock=clock
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "hey fakeyou halo"))
    await loop.wait_idle()
    clock.now += 25
    await loop.handle_transcript(event("two", "hey fakeyou lagi"))
    await loop.wait_idle()
    clock.now += 25
    await loop.handle_transcript(event("three", "masih aktif?"))
    await loop.wait_idle()
    assert len(responder.calls) == 3
    await loop.close()


def test_activation_window_rejects_nonpositive_values():
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(Responder(), activation_seconds=0)
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(Responder(), activation_seconds=float("inf"))


async def test_barge_in_stops_playback_only_while_speaking():
    responder, audio = Responder(), Audio()
    loop = ConversationLoop(responder, barge_in=True)
    loop.bind(audio)
    await loop.handle_transcript(event("one", "halo"))
    await loop.wait_idle()
    assert audio.stops == 0
    audio.speaking = True
    await loop.handle_transcript(event("two", "tunggu dulu"))
    await loop.wait_idle()
    assert audio.stops == 1
    assert audio.spoken[-1] == "tunggu dulu"
    await loop.close()


async def test_barge_in_disabled_never_stops_playback():
    responder, audio = Responder(), Audio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    audio.speaking = True
    await loop.handle_transcript(event("one", "halo"))
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
    loop.bind(Audio())
    await loop.handle_transcript(event("one"))
    with pytest.raises(ConversationError, match="conversation failed"):
        await loop.wait_idle()
    assert loop.phase == "failed"
    await loop.close()


async def test_agent_turn_journals_invoke_with_outcome():
    import json

    from fakeyou.journal import ProviderJournal

    lines: list[str] = []
    journal = ProviderJournal("meeting-1", emit=lines.append)

    class Agent:
        async def ainvoke(self, request, config):
            return {"messages": [AIMessage(content="spoken")]}

    responder = LangGraphResponder(Agent(), "thread", journal=journal)
    assert await responder.respond([event("one", "hi")], lambda: None) == "spoken"

    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["event"] == "agent_invoke"
    assert row["events"] == 1
    assert row["chars_out"] == len("spoken")
    assert row["duration_ms"] >= 0
    assert row["outcome"] == "answered"


async def test_loop_journals_turn_without_modifying_injected_responder():
    import json
    from dataclasses import dataclass

    from fakeyou.journal import ProviderJournal

    lines = []
    journal = ProviderJournal("meeting-1", emit=lines.append)

    @dataclass(frozen=True)
    class JournaledResponder:
        journal: object | None = None

        async def respond(self, events, on_tool_start):
            return "ok"

    loop = ConversationLoop(JournaledResponder(), journal=journal)

    loop.bind(Audio())
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert loop._responder.journal is None
    assert [json.loads(line)["event"] for line in lines] == ["agent_started", "agent_finished"]
    await loop.close()


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ("Dr. Andi hadir. Kami siap. Mulai sekarang.", "Dr. Andi hadir. Kami siap."),
        ("Nilainya 3.14. Proyek siap. Berikutnya mulai.", "Nilainya 3.14. Proyek siap."),
        ("Dia berkata, “Siap.” Kita mulai. Tunggu.", "Dia berkata, “Siap.” Kita mulai."),
        ("Satu.Dua.Tiga.", "Satu. Dua."),
    ],
)
def test_voice_cap_uses_language_aware_sentence_boundaries(reply, expected):
    assert cap_spoken_reply(reply) == expected
