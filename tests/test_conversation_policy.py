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
    _is_bare_ack,
    _request_messages,
)
from fakeyou.foundation.transcript import (
    TranscriptEvent,
)
from fakeyou.observability.diagnostics import Diagnostics
from tests._conversation_support import event
from tests.fakes import FakeClock, FakeReplyAudio, FakeResponder, RecordingJournal


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
        api_key=SecretStr("test-key"),
        base_url=f"http://127.0.0.1:{port}/v1",
        request_timeout=30,
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


async def test_addressed_text_replies_inside_wake_window_ack_runs(
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
        [first],
        ["rani twin, continue now"],
    ]
    [decided] = journal.events("ack_decided")
    assert decided["transcript_id"] == "t3"
    await loop.close()
