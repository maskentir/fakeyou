import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import SecretStr

from fakeyou.conversation.agent import (
    DEFAULT_PERSONA,
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
    create_meeting_agent,
)
from fakeyou.conversation.conversation import MeetingResponder
from fakeyou.conversation.meeting_messages import NoHistory, request_messages
from fakeyou.conversation.meeting_turns import (
    TurnGeneration,
    TurnInteraction,
    TurnTiming,
    build_meeting_turns,
)
from fakeyou.conversation.middleware import (
    patch_orphan_tool_calls,
    trim_messages_to_budget,
    unanswered_tool_calls,
)
from fakeyou.conversation.turn_events import (
    TurnEvent,
    TurnEventKind,
    TurnState,
    apply_arbitration,
    arbitrate,
)
from fakeyou.conversation.turn_graph import ActiveSpeech, TurnInput, record_barge_in
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.speakers import UNIDENTIFIED_PARTICIPANT
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.persona import PersonaLanguage
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.orchestration.persona_prompt import build_persona_prompt
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder, ScriptedJudge, ToolCallingFakeModel, judged
from tests._conversation_support import event
from tests._fakes_common import FakeClock
from tests._persistence_fakes import RecordingJournal


async def test_wake_mode_ignores_unaddressed_transcripts_but_records_them() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(wake_phrases=["hey fakeyou"]),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one", "our project status"))
    await loop.lifecycle.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    assert [item.text for item in loop.ingress.events] == ["our project status"]
    await loop.lifecycle.close()


async def test_barge_in_stops_playback_only_while_speaking() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder, interaction=TurnInteraction(barge_in=True))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.stops == 0
    audio.speaking = True
    await loop.ingress.receive_final(event("two", "hold on"))
    await loop.lifecycle.wait_idle()
    assert audio.stops == 1
    assert audio.spoken[-1] == "hold on"
    await loop.lifecycle.close()


async def test_barge_in_disabled_never_stops_playback() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder)
    loop.lifecycle.bind(audio)
    audio.speaking = True
    await loop.ingress.receive_final(event("one", "hello"))
    await loop.lifecycle.wait_idle()
    assert audio.stops == 0
    await loop.lifecycle.close()


async def test_interim_speech_interrupts_playback_after_enough_words(
    journal: RecordingJournal,
) -> None:

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)

    await loop.ingress.handle_interim("okay")
    await loop.ingress.handle_interim("okay wait")
    assert audio.stops == 0
    await loop.ingress.handle_interim("okay wait please")

    assert audio.stops == 1
    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert (row["trigger"], row["words"]) == ("interim", 3)


async def test_interim_speech_is_ignored_without_barge_in_or_playback() -> None:
    audio = FakeReplyAudio()
    audio.speaking = True
    quiet = build_meeting_turns(FakeResponder())
    quiet.lifecycle.bind(audio)
    await quiet.ingress.handle_interim("please stop for now")

    idle_audio = FakeReplyAudio()
    idle = build_meeting_turns(FakeResponder(), interaction=TurnInteraction(barge_in=True))
    idle.lifecycle.bind(idle_audio)
    await idle.ingress.handle_interim("please stop for now")

    assert audio.stops == 0
    assert idle_audio.stops == 0


async def test_interim_speech_over_the_twin_stops_it_in_wake_mode() -> None:
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(wake_phrases=["rani twin"], barge_in=True),
        timing=TurnTiming(clock=FakeClock()),
    )
    loop.lifecycle.bind(audio)

    await loop.ingress.handle_interim("someone else is talking now")

    assert audio.stops == 1
    await loop.lifecycle.close()


async def test_final_barge_in_is_journaled_with_its_trigger(
    journal: RecordingJournal,
) -> None:

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)

    await loop.ingress.receive_final(event("t1", "enough"))

    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert row["trigger"] == "final"
    await loop.lifecycle.close()


async def test_one_interruption_is_journaled_and_stopped_once(
    journal: RecordingJournal,
) -> None:

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)

    await loop.ingress.handle_interim("wait a second")
    await loop.ingress.handle_interim("wait a second please")

    assert audio.stops == 1
    assert len([line for line in lines if '"barge_in_detected"' in line]) == 1


async def test_interim_barge_in_rearms_for_the_next_reply() -> None:
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = build_meeting_turns(FakeResponder(), interaction=TurnInteraction(barge_in=True))
    loop.lifecycle.bind(audio)

    await loop.ingress.handle_interim("wait a second")
    await loop.ingress.receive_final(event("t1", "what is next"))
    await loop.lifecycle.wait_idle()
    audio.speaking = True
    audio.stops = 0
    await loop.ingress.handle_interim("hold on one moment")

    assert audio.spoken == ["what is next"]
    assert audio.stops == 1
    await loop.lifecycle.close()


async def test_local_transport_cancel_closes_loopback_without_fallback(
    journal: RecordingJournal,
) -> None:
    from langchain_openai import ChatOpenAI

    arrived = asyncio.Event()
    disconnected = asyncio.Event()
    connections = 0

    async def hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
    sockets = list(server.sockets or [])
    port = sockets[0].getsockname()[1]
    model = ChatOpenAI(
        model="loopback-dummy",
        api_key=SecretStr("test-key"),
        base_url=f"http://127.0.0.1:{port}/v1",
        timeout=30,
        max_retries=0,
    )
    agent = create_meeting_agent(model)
    loop = build_meeting_turns(
        MeetingResponder(agent, NoHistory(), journal=journal),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.lifecycle.bind(audio)
    try:
        await loop.ingress.receive_final(event("t1", "hello"))
        assert await asyncio.wait_for(arrived.wait(), 2)
        assert connections == 1
        await loop.lifecycle.close()
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


def test_policy_requires_configured_aliases() -> None:
    responder = FakeResponder()
    with pytest.raises(ValueError, match="twin aliases"):
        build_meeting_turns(responder, interaction=TurnInteraction(participant_policy_enabled=True))
    with pytest.raises(ValueError, match="twin aliases"):
        build_meeting_turns(
            responder,
            interaction=TurnInteraction(wake_phrases=["  "], participant_policy_enabled=True),
        )


async def test_policy_yields_the_floor_while_others_are_speaking(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    busy = [True]
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(
            wake_phrases=["Rani Twin"],
            participant_policy_enabled=True,
            others_speaking=lambda: busy[0],
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "Rani Twin, what is blocking the release?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert responder.calls == []
    [decision] = journal.events("policy_decided")
    assert (decision["action"], decision["reason"]) == ("silent", "floor_occupied")

    busy[0] = False
    await loop.ingress.receive_final(event("t2", "Rani Twin, what is blocking the release?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert len(responder.calls) == 1
    await loop.lifecycle.close()


async def test_policy_silent_makes_zero_responder_calls(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "Budi, when is the demo?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert responder.calls == []
    assert audio.spoken == []
    [decision] = journal.events("policy_decided")
    assert (decision["transcript_id"], decision["action"], decision["reason"]) == (
        "t1",
        "silent",
        "fallback_unnamed",
    )
    await loop.lifecycle.close()


async def test_policy_keeps_barge_in_on_speech_onset(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(
            wake_phrases=["Rani Twin"], barge_in=True, participant_policy_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t0", "Rani Twin, when is the demo?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    audio.speaking = True
    await loop.ingress.handle_interim("wait just a moment please")
    assert audio.stops == 1
    await loop.ingress.receive_final(event("t1", "Budi, when is the demo?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    await loop.lifecycle.close()


async def test_policy_answers_vocative_and_ignores_quote_mention(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "Rani Twin, when is the demo?"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    await loop.ingress.receive_final(
        event("t2", "He said Rani Twin agrees", participant_id="spaces/x/devices/9")
    )
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    assert audio.spoken == ["Rani Twin, when is the demo?"]
    actions = [row["action"] for row in journal.events("policy_decided")]
    assert actions == ["reply", "silent"]
    await loop.lifecycle.close()


def test_request_messages_carry_speaker_identity_without_owner_data() -> None:
    addressed = TranscriptEvent(
        "t1",
        datetime.now(UTC).isoformat(),
        "meeting_audio",
        "Rani Twin, hello",
        finalized=True,
        participant_id="spaces/x/devices/1",
        speaker_evidence_id="e1",
    )
    (known,) = request_messages([addressed])
    assert known.additional_kwargs["meeting_event"] == {
        "kind": "speech",
        "timestamp": addressed.timestamp,
        "source": "meeting_audio",
        "participant_id": "spaces/x/devices/1",
        "speaker_evidence_id": "e1",
        "stream_id": "",
        "speaker": "spaces/x/devices/1",
        "speaker_status": "observed",
    }
    (anonymous,) = request_messages([event("t2", "Hello")])
    assert anonymous.additional_kwargs["meeting_event"]["speaker_status"] == "unknown"
    assert anonymous.additional_kwargs["meeting_event"]["speaker"] == UNIDENTIFIED_PARTICIPANT


async def test_wake_phrase_must_match_a_whole_word() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(wake_phrases=["Rani"]),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one", "Ranier, what is the status?"))
    await loop.lifecycle.wait_idle()
    assert responder.calls == []
    await loop.ingress.receive_final(event("two", "Rani, what is the status?"))
    await loop.lifecycle.wait_idle()
    assert len(responder.calls) == 1
    await loop.lifecycle.close()


async def test_unaddressed_finals_stay_silent_when_only_the_twin_name_is_an_alias() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["Rani"]),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    for ident, text in (
        ("one", "Let's review the budget."),
        ("two", "Who owns the migration?"),
        ("three", "Sounds good to me."),
    ):
        await loop.ingress.receive_final(event(ident, text))
        await loop.lifecycle.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    await loop.ingress.receive_final(event("four", "Rani, what is blocking the release?"))
    await loop.lifecycle.wait_idle()
    assert len(responder.calls) == 1
    await loop.lifecycle.close()


async def test_explicit_wake_phrase_replaces_the_default_twin_name() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(wake_phrases=["hey fakeyou"], twin_names=["Rani"]),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one", "Rani, what is the status?"))
    await loop.lifecycle.wait_idle()
    assert responder.calls == []
    await loop.lifecycle.close()


async def test_unaddressed_speech_yields_the_floor_without_a_reply() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["Rani"], barge_in=True),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    audio.speaking = True
    await loop.ingress.receive_final(event("one", "let's review the budget"))
    await loop.lifecycle.wait_idle()
    assert responder.calls == []
    assert audio.stops == 1
    await loop.lifecycle.close()


async def test_a_backchannel_while_speaking_does_not_yield_the_floor() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    judge = ScriptedJudge(lambda _window: judged("stay_silent", backchannel=True))
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["Rani"], barge_in=True, judge=judge),
        timing=TurnTiming(clock=clock),
    )
    loop.lifecycle.bind(audio)
    audio.speaking = True
    await loop.ingress.receive_final(event("one", "yeah"))
    await loop.lifecycle.wait_idle()
    assert responder.calls == []
    assert audio.stops == 0
    await loop.lifecycle.close()


def _ev(kind: TurnEventKind, event_id: str, at: float) -> TurnEvent:
    return TurnEvent(kind=kind, event_id=event_id, at=at, payload="")


class ArbitrationJournal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append(fields)


def empty_turn_state() -> TurnState:
    return TurnState(last_event_id=None, kind=None)


def test_stop_beats_final_beats_interim_and_vad() -> None:
    result = arbitrate(
        [
            _ev("vad", "v1", 1.0),
            _ev("interim", "i1", 2.0),
            _ev("final", "f1", 3.0),
            _ev("stop", "s1", 0.5),
        ]
    )
    assert result.winner is not None
    assert result.winner.event_id == "s1"
    assert {e.event_id for e in result.journaled} == {"v1", "i1", "f1"}


def test_losing_events_are_journaled_without_touching_state() -> None:
    journal = ArbitrationJournal()
    before = empty_turn_state()
    result = arbitrate([_ev("final", "f1", 2.0), _ev("interim", "i1", 1.0)])
    after = apply_arbitration(before, result, journal)
    assert after["last_event_id"] == "f1"
    assert journal.names == ["turn_event_dropped"]
    assert journal.fields[0]["event_id"] == "i1"
    assert journal.fields[0]["winner_event_id"] == "f1"


def test_latest_event_wins_within_one_priority() -> None:
    result = arbitrate([_ev("final", "f1", 1.0), _ev("final", "f2", 2.0)])
    assert result.winner is not None
    assert result.winner.event_id == "f2"
    assert arbitrate([]).winner is None


def test_applied_winner_records_its_kind() -> None:
    journal = ArbitrationJournal()
    after = apply_arbitration(empty_turn_state(), arbitrate([_ev("stop", "s1", 1.0)]), journal)
    assert after["last_event_id"] == "s1"
    assert after["kind"] == "stop"


@pytest.mark.parametrize("language", ["id", "en"])
def test_persona_prompt_without_profile_is_the_default_meeting_prompt(
    language: PersonaLanguage,
) -> None:
    prompt = build_persona_prompt(None, language=language)
    assert prompt == f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE[language]}\n\n{ENDING_RULE}"
    assert build_persona_prompt(None, language="id") == DEFAULT_PERSONA


def assert_tool_pairs_intact(messages: Sequence[BaseMessage]) -> None:
    calls = {
        call["id"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    }
    answers = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    assert calls == answers


def tool_turn(index: int) -> list[AnyMessage]:
    call_id = f"call-{index}"
    return [
        HumanMessage(content=f"question {index} " + "context " * 20, id=f"h{index}"),
        AIMessage(
            content="",
            tool_calls=[{"name": "lookup", "args": {"query": "q"}, "id": call_id}],
        ),
        ToolMessage(content="result " * 30, tool_call_id=call_id),
        AIMessage(content=f"answer {index} " + "words " * 10),
    ]


def meeting_history_with_tools(*, turns: int) -> list[AnyMessage]:
    return [message for index in range(turns) for message in tool_turn(index)]


def test_trim_never_separates_tool_call_from_its_answer() -> None:
    history = meeting_history_with_tools(turns=20)
    for budget in (40, 120, 200, 333):
        trimmed = patch_orphan_tool_calls(trim_messages_to_budget(history, max_tokens=budget))
        assert_tool_pairs_intact(trimmed)
        assert trimmed[-1] is history[-1]


def test_unanswered_tool_calls_are_the_one_pairing_rule() -> None:
    history = meeting_history_with_tools(turns=2)
    orphan = AIMessage(
        content="", tool_calls=[{"name": "lookup", "args": {"query": "q"}, "id": "call-x"}]
    )
    assert unanswered_tool_calls([*history, orphan]) == ["call-x"]
    assert unanswered_tool_calls(history) == []
    patched = patch_orphan_tool_calls([*history, orphan])
    assert_tool_pairs_intact(patched)


def hanging_tool_agent(started: asyncio.Event) -> Any:
    @tool(description="Look something up slowly.")
    async def lookup(query: str) -> str:
        del query
        started.set()
        await asyncio.Event().wait()
        return "never"

    call = {"name": "lookup", "args": {"query": "q"}, "id": "call-hang", "type": "tool_call"}
    return create_meeting_agent(
        ToolCallingFakeModel(responses=[AIMessage(content="", tool_calls=[call])]),
        tools=[lookup],
    )


@pytest.mark.parametrize("mode", ["streamed", "whole_turn"])
async def test_stop_mid_tool_drains_the_turn_without_speaking(mode: str) -> None:
    started = asyncio.Event()
    agent = hanging_tool_agent(started)
    audio = FakeReplyAudio()
    turns = build_meeting_turns(
        MeetingResponder(agent, NoHistory(), language="en"),
        generation=TurnGeneration(sentence_streaming=mode == "streamed", language="en"),
    )
    turns.lifecycle.bind(audio)
    await turns.ingress.receive_final(event("f1", "check that please"))
    await asyncio.wait_for(started.wait(), 2)
    runtime = turns.lifecycle.runtime
    assert runtime is not None
    await turns.lifecycle.graph.ainvoke(
        TurnInput(
            event=TurnEvent(kind="stop", event_id="s1", at=2.0, payload=""),
            context=ParticipationContext(addressed=False),
            speaking_generation=turns.lifecycle.floor.generation,
        ),
        context=runtime,
    )
    await asyncio.wait_for(turns.lifecycle.drained(), 2)
    assert audio.spoken == []
    await turns.lifecycle.close()


async def test_a_barge_in_names_the_turn_it_interrupted() -> None:
    journal = RecordingJournal()
    speaking = asyncio.create_task(asyncio.sleep(0))
    active = ActiveSpeech(task=speaking, generation=1, started=0.0, transcript_id="t3")

    record_barge_in(journal, "interim", 4, decided_by="judge", interrupted=active)
    await speaking

    [row] = journal.events("barge_in_detected")
    assert row["interrupted_transcript_id"] == "t3"
