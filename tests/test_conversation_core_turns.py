import asyncio
from typing import Any, NoReturn, override

import pytest

from fakeyou.conversation.conversation import (
    ConversationLoop,
    LoopInteraction,
    cap_spoken_reply,
    settle_turn,
)
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


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


async def test_settle_turn_reports_a_completed_turn() -> None:
    assert await settle_turn(asyncio.create_task(asyncio.sleep(0))) is True


async def test_settle_turn_propagates_caller_cancellation() -> None:
    turn = asyncio.create_task(asyncio.sleep(30))
    waiter = asyncio.create_task(settle_turn(turn))
    await asyncio.sleep(0.01)
    waiter.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        turn.cancel()
        await asyncio.gather(turn, return_exceptions=True)


async def test_settle_turn_reports_a_turn_cancelled_on_its_own() -> None:
    turn = asyncio.create_task(asyncio.sleep(30))
    waiter = asyncio.create_task(settle_turn(turn))
    await asyncio.sleep(0.01)
    turn.cancel()
    assert await waiter is False
    assert turn.cancelled()


async def test_settle_turn_propagates_turn_errors() -> None:
    async def boom() -> None:
        raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await settle_turn(asyncio.create_task(boom()))


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
        del args
        raise RuntimeError("private-provider-data")

    responder, audio = FakeResponder(), FakeReplyAudio()
    loop = ConversationLoop(responder, interaction=LoopInteraction(on_transition=broken))
    loop.bind(audio)
    await loop.handle_transcript(event("one"))
    await loop.wait_idle()
    assert len(responder.calls) == 1
    assert len(audio.spoken) == 1
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
