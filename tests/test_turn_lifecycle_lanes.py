import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.meeting_turns import (
    MeetingTurns,
    TurnInteraction,
    TurnTiming,
    build_meeting_turns,
)
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import PlannedTurn
from fakeyou.foundation.transcript import EventSource, TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.session.commands import CommandOutcome, CommandSpec
from fakeyou.session.intent import Reading, intent_of
from fakeyou.session.planner import plan_turn
from fakeyou.session.policy import Actor, Channel, Grant, authorize
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal

TWIN = "Rani Twin"


class FakeChat:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def run(
        self, spec: CommandSpec, *, plan_id: str, ordinal: int, actor: str
    ) -> CommandOutcome:
        self.sent.append((spec.text, actor))
        return CommandOutcome(f"{plan_id}:{ordinal}", spec.kind, "effect_observed")


def chat_permitted(event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
    actor = Actor("participant", event.participant_id)
    authorization = authorize(
        intent_of(reading), actor=actor, channel=channel, grant=Grant(frozenset({"meet_chat"}))
    )
    plan = plan_turn(authorization, channel=channel, plan_id=f"plan-{event.id}")
    return PlannedTurn(plan, actor=event.participant_id)


class FixedResponder(FakeResponder):
    def __init__(self, reply: str) -> None:
        super().__init__()
        self._reply = reply

    @override
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        await super().respond(events, on_tool_start)
        return SpokenChunk(text=self._reply, language=DEFAULTS.speech.language)


def attention(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id="attention-1",
        timestamp=datetime.now(UTC).isoformat(),
        source="meeting_attention",
        text=text,
        finalized=True,
        participant_id="unknown",
        speaker_evidence_id=None,
    )


def meeting_input(event_id: str, text: str, source: EventSource) -> TranscriptEvent:
    return TranscriptEvent(
        event_id,
        datetime.now(UTC).isoformat(),
        source,
        text,
        finalized=True,
        participant_id="device-budi",
        speaker_evidence_id=None,
    )


def lanes(
    responder: FakeResponder,
    *,
    interaction: TurnInteraction | None = None,
    timing: TurnTiming | None = None,
    journal: RecordingJournal | None = None,
) -> tuple[MeetingTurns, FakeReplyAudio]:
    audio = FakeReplyAudio()
    turns = build_meeting_turns(
        responder,
        interaction=interaction or TurnInteraction(twin_names=(TWIN,)),
        timing=timing or TurnTiming(quiet_gap_seconds=0.0),
        diagnostics=Diagnostics(journal=journal or RecordingJournal()),
    )
    turns.lifecycle.bind(audio)
    return turns, audio


def interaction(**options: Any) -> TurnInteraction:
    return TurnInteraction(twin_names=(TWIN,), **options)


async def settle(turns: MeetingTurns) -> None:
    for _ in range(50):
        await turns.lifecycle.wait_idle()
        if not turns.lifecycle.deferred.pending and not turns.lifecycle.is_busy:
            return
        await asyncio.sleep(0.02)


async def test_a_chat_that_names_the_twin_is_answered_in_chat() -> None:
    chat = FakeChat()
    turns, audio = lanes(
        FixedResponder("Here is the deck link."),
        interaction=interaction(commands=chat, planning=chat_permitted),
    )

    accepted = turns.ingress.receive_chat(
        meeting_input("c1", f"{TWIN}, can you share the deck?", "meeting_chat")
    )
    await settle(turns)

    assert accepted
    assert chat.sent == [("Here is the deck link.", "device-budi")]
    assert audio.spoken == []
    await turns.close()


async def test_the_runtime_can_hand_a_chat_line_to_the_meeting_turns() -> None:
    chat = FakeChat()
    turns, _ = lanes(
        FixedResponder("Here is the deck link."),
        interaction=interaction(commands=chat, planning=chat_permitted),
    )

    accepted = turns.receive_chat(
        meeting_input("c1", f"{TWIN}, can you share the deck?", "meeting_chat")
    )
    await settle(turns)

    assert accepted
    assert chat.sent == [("Here is the deck link.", "device-budi")]
    await turns.close()


async def test_the_runtime_can_offer_attention_to_the_meeting_turns() -> None:
    turns, _ = lanes(FixedResponder("On it."))

    accepted = turns.receive_attention(
        meeting_input("a1", "A participant said in chat: the deck is ready", "meeting_attention"),
        channel="voice",
    )
    await settle(turns)

    assert accepted
    await turns.close()


async def test_a_chat_that_does_not_name_the_twin_is_left_alone() -> None:
    chat = FakeChat()
    responder = FakeResponder()
    turns, _ = lanes(responder, interaction=interaction(commands=chat, planning=chat_permitted))

    accepted = turns.ingress.receive_chat(
        meeting_input("c1", "anyone has the deck?", "meeting_chat")
    )
    await settle(turns)

    assert not accepted
    assert responder.calls == []
    assert chat.sent == []
    await turns.close()


async def test_chat_replies_never_carry_internal_references() -> None:
    chat = FakeChat()
    turns, _ = lanes(
        FixedResponder("The roadmap [screen#3] starts in Q1."),
        interaction=interaction(commands=chat, planning=chat_permitted),
    )

    turns.ingress.receive_chat(meeting_input("c1", f"{TWIN} when does it start?", "meeting_chat"))
    await settle(turns)

    assert chat.sent == [("The roadmap starts in Q1.", "device-budi")]
    await turns.close()


async def test_without_a_chat_channel_the_reply_is_dropped_not_spoken() -> None:
    journal = RecordingJournal()
    turns, audio = lanes(
        FixedResponder("Sure."), journal=journal, interaction=interaction(planning=chat_permitted)
    )

    turns.ingress.receive_chat(meeting_input("c1", f"{TWIN} ping", "meeting_chat"))
    await settle(turns)

    assert audio.spoken == []
    assert journal.events("chat_reply_dropped")[0]["reason"] == "no_chat_channel"
    await turns.close()


async def test_a_voice_interjection_waits_for_a_quiet_floor() -> None:
    turns, audio = lanes(
        FixedResponder("The beta date looks wrong."), timing=TurnTiming(quiet_gap_seconds=0.15)
    )

    turns.lifecycle.note_activity()
    turns.ingress.receive_attention(
        meeting_input("a1", "attention screen#2", "meeting_attention"), channel="voice"
    )
    await asyncio.sleep(0.05)
    early = list(audio.spoken)
    await settle(turns)

    assert early == []
    assert audio.spoken == ["The beta date looks wrong."]
    await turns.close()


async def test_a_voice_interjection_never_talks_over_someone() -> None:
    journal = RecordingJournal()
    turns, audio = lanes(
        FixedResponder("unused"),
        journal=journal,
        interaction=interaction(others_speaking=lambda: True),
        timing=TurnTiming(quiet_gap_seconds=0.0, deferred_ttl_seconds=0.1),
    )

    turns.ingress.receive_attention(
        meeting_input("a1", "attention", "meeting_attention"), channel="voice"
    )
    await asyncio.sleep(0.4)
    turns.lifecycle.start_pending()
    await settle(turns)

    assert audio.spoken == []
    assert [row["transcript_id"] for row in journal.events("deferred_turn_expired")] == ["a1"]
    await turns.close()


async def test_speech_preempts_a_deferred_turn_which_is_retried_once() -> None:
    gate = asyncio.Event()
    responder = FakeResponder(gate)
    turns, audio = lanes(responder)

    turns.ingress.receive_attention(
        meeting_input("a1", "noticed", "meeting_attention"), channel="voice"
    )
    await responder.started.wait()
    await turns.ingress.receive_final(event("t1", f"{TWIN}, quick question"))
    gate.set()
    await settle(turns)

    assert audio.spoken == [f"{TWIN}, quick question", "noticed"]
    assert [[item.id for item in call] for call in responder.calls] == [["a1"], ["t1"], ["a1"]]
    await turns.close()


async def test_shadow_mode_suppresses_deferred_turns() -> None:
    journal = RecordingJournal()
    turns, audio = lanes(FakeResponder(), journal=journal, interaction=interaction(shadow=True))

    accepted = turns.ingress.receive_attention(
        meeting_input("a1", "noticed", "meeting_attention"), channel="voice"
    )
    await settle(turns)

    assert not accepted
    assert audio.spoken == []
    assert journal.events("deferred_turn_suppressed")[0]["reason"] == "shadow"
    await turns.close()


async def test_spoken_sentences_are_scrubbed_of_internal_references() -> None:
    turns, audio = lanes(FixedResponder("Per screen#1, beta is on 17 March."))

    await turns.ingress.receive_final(event("t1", f"{TWIN}, when is beta?"))
    await settle(turns)

    assert audio.spoken == ["Per, beta is on 17 March."]
    await turns.close()


async def test_closing_drops_waiting_deferred_turns() -> None:
    turns, audio = lanes(FakeResponder(), interaction=interaction(others_speaking=lambda: True))
    turns.ingress.receive_attention(
        meeting_input("a1", "noticed", "meeting_attention"), channel="voice"
    )

    await turns.close()

    assert not turns.lifecycle.deferred.pending
    assert audio.spoken == []


async def test_the_same_attention_is_never_offered_twice() -> None:
    turns, _ = lanes(FixedResponder("Worth saying."))

    first = turns.receive_attention(
        attention("The shared screen now shows the launch slide."), channel="voice"
    )
    second = turns.receive_attention(
        attention("The shared screen now shows the launch slide."), channel="voice"
    )

    assert first is True
    assert second is False
    await turns.close()


async def test_a_changed_attention_is_offered() -> None:
    turns, _ = lanes(FixedResponder("Worth saying."))

    first = turns.receive_attention(
        attention("The shared screen now shows the launch slide."), channel="voice"
    )
    await settle(turns)
    second = turns.receive_attention(
        attention("The shared screen now shows the pricing slide."), channel="voice"
    )

    assert first is True
    assert second is True
    await turns.close()
