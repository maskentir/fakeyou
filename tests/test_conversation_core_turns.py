import asyncio
from typing import Any, NoReturn, override

import pytest

from fakeyou.conversation.conversation import cap_spoken_reply
from fakeyou.conversation.meeting_turns import TurnInteraction, build_meeting_turns
from fakeyou.conversation.turn_graph import SpeechFloor
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder, ScriptedJudge, judged
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


async def test_new_final_cancels_waiting_model_and_answers_newest_once(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.ingress.receive_final(event("t2", "second"))
    assert responder.cancelled.is_set()
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
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
    await loop.lifecycle.close()


async def test_buffers_before_bind_and_ignores_duplicates_interims_and_empty_text() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder)
    await loop.ingress.receive_final(event("provisional", finalized=False))
    await loop.ingress.receive_final(event("empty", "  "))
    await loop.ingress.receive_final(event("one", "kept"))
    await loop.ingress.receive_final(event("one", "kept"))
    assert responder.calls == []
    loop.lifecycle.bind(audio)
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["kept"]
    assert len(loop.ingress.events) == 1
    await loop.lifecycle.close()
    await loop.ingress.receive_final(event("after-close"))
    assert len(responder.calls) == 1


async def test_close_cancels_model_and_prevents_later_speech() -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(responder)
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one"))
    await responder.started.wait()
    await loop.lifecycle.close()
    await loop.lifecycle.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []


async def test_floor_hold_reports_a_completed_turn() -> None:
    assert await SpeechFloor().hold(asyncio.sleep(0)) is True


async def test_floor_hold_propagates_caller_cancellation() -> None:
    floor = SpeechFloor()
    waiter = asyncio.create_task(floor.hold(asyncio.sleep(30)))
    await asyncio.sleep(0.01)
    active = floor.active
    assert active is not None
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        active.task.cancel()
        await asyncio.gather(active.task, return_exceptions=True)


async def test_floor_hold_reports_a_turn_cancelled_on_its_own() -> None:
    floor = SpeechFloor()
    waiter = asyncio.create_task(floor.hold(asyncio.sleep(30)))
    await asyncio.sleep(0.01)
    active = floor.active
    assert active is not None
    active.task.cancel()
    assert await waiter is False
    assert active.task.cancelled()


async def test_floor_hold_propagates_turn_errors() -> None:
    async def boom() -> None:
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await SpeechFloor().hold(boom())


async def test_loop_speaks_capped_reply_head() -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder)
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one", "One. Two. Three. Four."))
    await loop.lifecycle.wait_idle()
    assert audio.spoken == ["One. Two."]
    await loop.lifecycle.close()


async def test_observer_failure_preserves_model_and_speech() -> None:
    def broken(*args: Any) -> NoReturn:
        del args
        raise RuntimeError("private-provider-data")

    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(responder, interaction=TurnInteraction(on_transition=broken))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("one"))
    await loop.lifecycle.wait_idle()
    assert len(responder.calls) == 1
    assert len(audio.spoken) == 1
    await loop.lifecycle.close()


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


class GatedAudio(FakeReplyAudio):
    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self.gate = gate
        self.entered = asyncio.Event()
        self.attempts = 0

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        self.attempts += 1
        self.entered.set()
        await self.gate.wait()
        await super().speak(
            text,
            sentence_index=sentence_index,
            transcript_id=transcript_id,
            language=language,
        )
        del language


async def test_interim_cancels_pre_pcm_child_without_answering(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.ingress.handle_interim("one moment")
    assert not responder.cancelled.is_set()
    await loop.ingress.handle_interim("please hold on a moment")
    assert responder.cancelled.is_set()
    assert len(responder.calls) == 1
    assert audio.spoken == []
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "interim"
    assert (requested["request_id"], requested["transcript_id"]) == ("t1:1", "t1")
    assert journal.events("barge_in_detected") != []
    await loop.ingress.receive_final(event("t2", "second"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert audio.spoken == ["second"]
    assert journal.events("agent_failed") == []
    await loop.lifecycle.close()


async def test_duplicate_final_and_interim_do_not_reschedule(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(barge_in=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.ingress.receive_final(event("t1", "first"))
    assert not responder.cancelled.is_set()
    assert len(responder.calls) == 1
    await loop.ingress.handle_interim("please hold on a moment")
    assert responder.cancelled.is_set()
    await loop.ingress.handle_interim("please hold on a moment")
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "interim"
    assert len(responder.calls) == 1
    await loop.ingress.receive_final(event("t2", "second"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert audio.spoken == ["second"]
    assert [[item.text for item in call] for call in responder.calls] == [
        ["first"],
        ["second"],
    ]
    await loop.lifecycle.close()


async def test_close_cancels_active_turn_and_never_reschedules(journal: RecordingJournal) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.lifecycle.close()
    assert responder.cancelled.is_set()
    assert audio.spoken == []
    assert loop.lifecycle.phase == "closed"
    await loop.lifecycle.close()
    await loop.ingress.receive_final(event("t2", "second"))
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
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(audio.entered.wait(), 2)
    await loop.ingress.receive_final(event("t2", "second"))
    gate.set()
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert audio.attempts == 2
    assert audio.spoken == ["second"]
    assert loop.lifecycle.phase == "listening"
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "new_final"
    assert journal.events("agent_failed") == []
    await loop.lifecycle.close()


async def test_close_during_tts_speak_drops_audio_and_stays_closed(
    journal: RecordingJournal,
) -> None:
    gate = asyncio.Event()
    responder, audio = FakeResponder(), GatedAudio(gate)
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(audio.entered.wait(), 2)
    await loop.lifecycle.close()
    gate.set()
    assert audio.spoken == []
    assert loop.lifecycle.phase == "closed"
    await loop.lifecycle.close()
    [requested] = journal.events("agent_cancel_requested")
    assert requested["reason"] == "close"
    assert journal.events("agent_failed") == []


async def test_cancelled_request_ids_do_not_attach_to_next_transcript(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(asyncio.Event()), FakeReplyAudio()
    loop = build_meeting_turns(responder, diagnostics=Diagnostics(journal=journal))
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "first"))
    assert await asyncio.wait_for(responder.started.wait(), 2)
    await loop.ingress.receive_final(event("t2", "second"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    started = [row["request_id"] for row in journal.events("agent_started")]
    assert started == ["t1:1", "t2:2"]
    finished = [row["request_id"] for row in journal.events("agent_finished")]
    assert finished == ["t2:2"]
    [cancelled] = [row for row in journal.events("agent_cancelled") if row["request_id"] == "t1:1"]
    assert (cancelled["transcript_id"], cancelled["generation"]) == ("t1", 1)
    body = "\n".join(journal.lines)
    assert "first" not in body
    assert "second" not in body
    await loop.lifecycle.close()


async def test_shadow_mode_decides_without_speaking(
    journal: RecordingJournal,
) -> None:
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["harun"], shadow=True),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.receive_final(event("t1", "Harun, status update please", participant_id="a"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert responder.calls == []
    assert audio.spoken == []
    assert [row["reason"] for row in journal.events("policy_decided")] == ["fallback_named"]
    assert [row["reason"] for row in journal.events("transcript_suppressed")] == ["shadow"]
    await loop.lifecycle.close()


async def test_interim_judgement_answers_the_final_without_blocking(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["harun"], judge=judge),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(audio)
    await loop.ingress.handle_interim("what is next")
    judging = loop.lifecycle.judging
    assert judging is not None
    await asyncio.wait_for(judging.cache.wait_idle(), 2)
    await loop.ingress.receive_final(event("t2", "What is next?", participant_id="b"))
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)
    assert [row["reason"] for row in journal.events("policy_decided")] == ["judge_reply"]
    assert len(judge.windows) == 1
    assert audio.spoken == ["What is next?"]
    await loop.lifecycle.close()
