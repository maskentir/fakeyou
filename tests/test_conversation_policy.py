import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr

from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.conversation import (
    BARE_ACK_VOCABULARY,
    ConversationLoop,
    LangGraphResponder,
    LoopInteraction,
    LoopTiming,
    is_bare_ack,
    is_interjection,
    request_messages,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event, voiced_event
from tests._fakes_common import FakeClock
from tests._persistence_fakes import RecordingJournal


async def test_wake_mode_ignores_unaddressed_transcripts_but_records_them() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["hey fakeyou"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "our project status"))
    await loop.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    assert [item.text for item in loop.events] == ["our project status"]
    await loop.close()


async def test_wake_phrase_activates_window_case_insensitively() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["hey fakeyou"]),
        timing=LoopTiming(activation_seconds=30, clock=clock),
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


async def test_wake_phrase_inside_window_extends_activation() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["hey fakeyou"]),
        timing=LoopTiming(activation_seconds=30, clock=clock),
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


def test_activation_window_rejects_nonpositive_values() -> None:
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(FakeResponder(), timing=LoopTiming(activation_seconds=0))
    with pytest.raises(ValueError, match="activation window"):
        ConversationLoop(FakeResponder(), timing=LoopTiming(activation_seconds=float("inf")))


async def test_barge_in_stops_playback_only_while_speaking() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, interaction=LoopInteraction(barge_in=True))
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


async def test_barge_in_disabled_never_stops_playback() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder)
    loop.bind(audio)
    audio.speaking = True
    await loop.handle_transcript(event("one", "hello"))
    await loop.wait_idle()
    assert audio.stops == 0
    await loop.close()


async def test_interim_speech_interrupts_playback_after_enough_words(
    journal: RecordingJournal,
) -> None:
    import json

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        FakeResponder(),
        interaction=LoopInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)

    await loop.handle_interim("okay")
    await loop.handle_interim("okay wait")
    assert audio.stops == 0
    await loop.handle_interim("okay wait please")

    assert audio.stops == 1
    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert (row["trigger"], row["words"]) == ("interim", 3)


async def test_interim_speech_is_ignored_without_barge_in_or_playback() -> None:
    audio = FakeReplyAudio()
    audio.speaking = True
    quiet = ConversationLoop(FakeResponder())
    quiet.bind(audio)
    await quiet.handle_interim("please stop for now")

    idle_audio = FakeReplyAudio()
    idle = ConversationLoop(FakeResponder(), interaction=LoopInteraction(barge_in=True))
    idle.bind(idle_audio)
    await idle.handle_interim("please stop for now")

    assert audio.stops == 0
    assert idle_audio.stops == 0


async def test_interim_barge_in_respects_wake_phrases() -> None:
    clock = FakeClock()
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        FakeResponder(),
        interaction=LoopInteraction(wake_phrases=["rani twin"], barge_in=True),
        timing=LoopTiming(clock=clock),
    )
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
    journal: RecordingJournal,
) -> None:
    import json

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        FakeResponder(),
        interaction=LoopInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)

    await loop.handle_transcript(event("t1", "enough"))

    [row] = [json.loads(line) for line in lines if '"barge_in_detected"' in line]
    assert row["trigger"] == "final"
    await loop.close()


async def test_one_interruption_is_journaled_and_stopped_once(
    journal: RecordingJournal,
) -> None:

    lines = journal.lines
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(
        FakeResponder(),
        interaction=LoopInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)

    await loop.handle_interim("wait a second")
    await loop.handle_interim("wait a second please")

    assert audio.stops == 1
    assert len([line for line in lines if '"barge_in_detected"' in line]) == 1


async def test_interim_barge_in_rearms_for_the_next_reply() -> None:
    audio = FakeReplyAudio()
    audio.speaking = True
    loop = ConversationLoop(FakeResponder(), interaction=LoopInteraction(barge_in=True))
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
    agent = create_meeting_agent(model, checkpointer=InMemorySaver())
    loop = ConversationLoop(
        LangGraphResponder(agent, "loopback-thread", journal=journal),
        diagnostics=Diagnostics(journal=journal),
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


def test_policy_requires_configured_aliases() -> None:
    responder = FakeResponder()
    with pytest.raises(ValueError, match="twin aliases"):
        ConversationLoop(responder, interaction=LoopInteraction(participant_policy_enabled=True))
    with pytest.raises(ValueError, match="twin aliases"):
        ConversationLoop(
            responder,
            interaction=LoopInteraction(wake_phrases=["  "], participant_policy_enabled=True),
        )


async def test_policy_yields_the_floor_while_others_are_speaking(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    busy = [True]
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(
            wake_phrases=["Rani Twin"],
            participant_policy_enabled=True,
            others_speaking=lambda: busy[0],
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani Twin, what is blocking the release?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert responder.calls == []
    [decision] = journal.events("policy_decided")
    assert (decision["action"], decision["reason"]) == ("silent", "floor_occupied")

    busy[0] = False
    await loop.handle_transcript(event("t2", "Rani Twin, what is blocking the release?"))
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert len(responder.calls) == 1
    await loop.close()


async def test_policy_silent_makes_zero_responder_calls(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_keeps_barge_in_on_speech_onset(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(
            wake_phrases=["Rani Twin"], barge_in=True, participant_policy_enabled=True
        ),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_answers_vocative_and_ignores_quote_mention(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_unknown_followup_does_not_inherit_activation(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_unknown_followup_after_unknown_vocative_stays_silent(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_other_known_speaker_followup_stays_silent(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
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


async def test_policy_same_speaker_followup_replies(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
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
        "timestamp": addressed.timestamp,
        "source": "meeting_audio",
        "participant_id": "spaces/x/devices/1",
        "speaker_evidence_id": "e1",
        "stream_id": "",
        "speaker_status": "observed",
    }
    (anonymous,) = request_messages([event("t2", "Hello")])
    assert anonymous.additional_kwargs["meeting_event"]["speaker_status"] == "unknown"


ACK_CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "ack-cases.json").read_text(encoding="utf-8")
)


def test_bare_ack_vocabulary_is_pinned() -> None:
    assert BARE_ACK_VOCABULARY == frozenset(ACK_CASES["vocabulary"])


@pytest.mark.parametrize("text", ACK_CASES["observed_bare"] + ACK_CASES["isolated"])
def test_observed_single_token_acks_match_exactly(text: str) -> None:
    assert is_bare_ack(text)


@pytest.mark.parametrize("text", ACK_CASES["replies"] + ACK_CASES["blanks"])
def test_substantive_text_is_never_a_bare_ack(text: str) -> None:
    assert not is_bare_ack(text)


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


async def test_bare_ack_run_suppresses_after_first(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
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


async def test_ack_suppression_journals_a_silent_decision(journal: RecordingJournal) -> None:
    loop = ConversationLoop(FakeResponder(), diagnostics=Diagnostics(journal=journal))
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
async def test_substantive_text_replies_inside_ack_runs(
    journal: RecordingJournal, text: str
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
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


async def test_substantive_text_breaks_the_ack_run(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
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


async def test_vocative_text_replies_inside_ack_runs(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, diagnostics=Diagnostics(journal=journal))
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


async def test_bare_acks_after_a_statement_stay_silent_inside_the_window(
    journal: RecordingJournal,
) -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["rani twin"]),
        timing=LoopTiming(activation_seconds=30, clock=clock),
        diagnostics=Diagnostics(journal=journal),
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
        ["rani twin, continue now"],
    ]
    silent = {
        row["transcript_id"]: row["reason"]
        for row in journal.events("policy_decided")
        if row["action"] == "silent"
    }
    assert silent == {"t2": "bare_ack", "t3": "bare_ack"}
    await loop.close()


async def test_bare_ack_answers_a_question_the_twin_just_asked(journal: RecordingJournal) -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["rani twin"]),
        timing=LoopTiming(activation_seconds=30, clock=clock),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    answer, after = ACK_CASES["run"][0], ACK_CASES["run"][1]
    await loop.handle_transcript(event("t1", "rani twin, what do you need?"))
    await loop.wait_idle()
    await loop.handle_transcript(event("t2", answer))
    await loop.wait_idle()
    await loop.handle_transcript(event("t3", after))
    await loop.wait_idle()
    assert [[item.text for item in call] for call in responder.calls] == [
        ["rani twin, what do you need?"],
        [answer],
    ]
    await loop.close()


async def test_wake_phrase_must_match_a_whole_word() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "Ranier, what is the status?"))
    await loop.wait_idle()
    assert responder.calls == []
    await loop.handle_transcript(event("two", "Rani, what is the status?"))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.close()


async def test_unaddressed_finals_stay_silent_when_only_the_twin_name_is_an_alias() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    for ident, text in (
        ("one", "Let's review the budget."),
        ("two", "Who owns the migration?"),
        ("three", "Sounds good to me."),
    ):
        await loop.handle_transcript(event(ident, text))
        await loop.wait_idle()
    assert responder.calls == []
    assert audio.spoken == []
    await loop.handle_transcript(event("four", "Rani, what is blocking the release?"))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    await loop.close()


async def test_explicit_wake_phrase_replaces_the_default_twin_name() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["hey fakeyou"], twin_names=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("one", "Rani, what is the status?"))
    await loop.wait_idle()
    assert responder.calls == []
    await loop.close()


async def test_unaddressed_speech_yields_the_floor_without_a_reply() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"], barge_in=True),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    audio.speaking = True
    await loop.handle_transcript(event("one", "let's review the budget"))
    await loop.wait_idle()
    assert responder.calls == []
    assert audio.stops == 1
    await loop.close()


async def _assert_speaking_twin_keeps_the_floor(text: str) -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"], barge_in=True),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    audio.speaking = True
    await loop.handle_transcript(event("one", text))
    await loop.wait_idle()
    assert responder.calls == []
    assert audio.stops == 0
    await loop.close()


@pytest.mark.parametrize("text", ACK_CASES["vocabulary"])
async def test_bare_ack_while_speaking_does_not_yield_the_floor(text: str) -> None:
    await _assert_speaking_twin_keeps_the_floor(text)


@pytest.mark.parametrize("text", ACK_CASES["backchannels"])
def test_english_backchannels_are_bare_acks(text: str) -> None:
    assert is_bare_ack(text)


@pytest.mark.parametrize("text", ACK_CASES["backchannels"])
async def test_backchannel_while_speaking_does_not_yield_the_floor(text: str) -> None:
    await _assert_speaking_twin_keeps_the_floor(text)


async def test_bare_ack_inside_the_window_is_not_answered() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani, the status is green.", participant_id="a"))
    await loop.wait_idle()
    clock.now += 5
    await loop.handle_transcript(event("t2", ACK_CASES["run"][0], participant_id="a"))
    await loop.wait_idle()

    assert len(responder.calls) == 1
    await loop.close()


async def test_interjection_inside_the_window_is_silent_and_yields_the_floor() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"], barge_in=True),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani, what is the status?", participant_id="a"))
    await loop.wait_idle()
    audio.speaking = True
    clock.now += 5
    await loop.handle_transcript(event("t2", ACK_CASES["interjections"][0], participant_id="a"))
    await loop.wait_idle()

    assert len(responder.calls) == 1
    assert audio.stops == 1
    await loop.close()


async def test_unnamed_follow_up_from_the_same_speaker_is_answered() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani, what is the status?", participant_id="a"))
    await loop.wait_idle()
    clock.now += 5
    await loop.handle_transcript(event("t2", ACK_CASES["unnamed_questions"][0], participant_id="a"))
    await loop.wait_idle()

    assert len(responder.calls) == 2
    await loop.close()


async def test_unnamed_turn_from_another_speaker_inside_the_window_is_silent() -> None:
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani"]),
        timing=LoopTiming(clock=clock),
    )
    loop.bind(audio)
    await loop.handle_transcript(event("t1", "Rani, what is the status?", participant_id="a"))
    await loop.wait_idle()
    clock.now += 5
    await loop.handle_transcript(event("t2", ACK_CASES["unnamed_questions"][0], participant_id="b"))
    await loop.wait_idle()

    assert len(responder.calls) == 1
    await loop.close()


LIVE_ADDRESSING_LABELS = json.loads(
    (Path(__file__).parent / "fixtures" / "addressing-labels-kyf-live-3.json").read_text(
        encoding="utf-8"
    )
)


async def test_live_turns_replay_with_the_labelled_answers_and_reasons(
    journal: RecordingJournal,
) -> None:
    labels = LIVE_ADDRESSING_LABELS
    responder, audio, clock = FakeResponder(), FakeReplyAudio(), FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=labels["twin_names"], barge_in=labels["barge_in"]),
        timing=LoopTiming(clock=clock),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    for index, turn in enumerate(labels["turns"]):
        clock.now = turn["at"]
        before = len(responder.calls)
        await loop.handle_transcript(
            event(f"t{index}", turn["text"], participant_id=turn["speaker"])
        )
        await loop.wait_idle()
        assert (len(responder.calls) > before) is turn["answer"], turn["text"]
    decided = [(row["transcript_id"], row["reason"]) for row in journal.events("policy_decided")]
    assert decided == [(f"t{index}", turn["reason"]) for index, turn in enumerate(labels["turns"])]
    await loop.close()


def test_interjection_detection_covers_the_fixture_openers() -> None:
    for text in ACK_CASES["interjections"]:
        assert is_interjection(text)
    for text in ACK_CASES["non_interjections"]:
        assert not is_interjection(text)


async def test_policy_followup_from_another_voice_stays_silent(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(
        voiced_event(
            "t1", "Rani Twin, when is the demo?", participant_id="speaker-a", voice_clusters=("A",)
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(
        voiced_event("t2", "Anyone want coffee?", participant_id="speaker-a", voice_clusters=("B",))
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    reasons = [row["reason"] for row in journal.events("policy_decided")]
    assert reasons == ["addressed", "not_addressed"]
    await loop.close()


async def test_policy_followup_from_the_engaged_voice_replies(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(
        voiced_event(
            "t1", "Rani Twin, when is the demo?", participant_id="speaker-a", voice_clusters=("A",)
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 5.0
    await loop.handle_transcript(
        voiced_event("t2", "What time exactly?", participant_id="speaker-a", voice_clusters=("A",))
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == [
        "Rani Twin, when is the demo?",
        "What time exactly?",
    ]
    assert [row["reason"] for row in journal.events("policy_decided")] == [
        "addressed",
        "followup_same_speaker",
    ]
    await loop.close()


async def test_policy_chatter_between_other_voices_does_not_extend_the_window(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(wake_phrases=["Rani Twin"], participant_policy_enabled=True),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(
        voiced_event(
            "t1", "Rani Twin, when is the demo?", participant_id="speaker-a", voice_clusters=("A",)
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    for index, text in enumerate(("blueberry or new?", "the sugar is fine", "no, the other one")):
        clock.now += 1.0
        await loop.handle_transcript(
            voiced_event(f"c{index}", text, participant_id="speaker-a", voice_clusters=("B",))
        )
        await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == ["Rani Twin, when is the demo?"]
    assert [row["reason"] for row in journal.events("policy_decided")] == [
        "addressed",
        "not_addressed",
        "not_addressed",
        "not_addressed",
    ]
    await loop.close()


async def test_name_addressed_policy_silences_other_voices_in_one_slot(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    clock = FakeClock()
    loop = ConversationLoop(
        responder,
        interaction=LoopInteraction(twin_names=["Rani Twin"]),
        timing=LoopTiming(clock=clock.monotonic),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.bind(audio)
    await loop.handle_transcript(
        voiced_event(
            "t1", "Rani Twin, when is the demo?", participant_id="speaker-a", voice_clusters=("A",)
        )
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 2.0
    await loop.handle_transcript(
        voiced_event("t2", "Anyone want coffee?", participant_id="speaker-a", voice_clusters=("B",))
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    clock.now += 2.0
    await loop.handle_transcript(
        voiced_event("t3", "What time exactly?", participant_id="speaker-a", voice_clusters=("A",))
    )
    await asyncio.wait_for(loop.wait_idle(), 2)
    assert [call[0].text for call in responder.calls] == [
        "Rani Twin, when is the demo?",
        "What time exactly?",
    ]
    assert [row["reason"] for row in journal.events("policy_decided")] == [
        "addressed",
        "not_addressed",
        "followup_same_speaker",
    ]
    await loop.close()
