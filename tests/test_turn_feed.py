import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, override

import pytest

from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.config.identifiers import SessionId
from fakeyou.foundation.transcript import TranscriptEvent, TranscriptUpdate
from fakeyou.orchestration import kernel_port
from fakeyou.orchestration.kernel_port import KernelPort, TurnSignal
from fakeyou.orchestration.transcript import (
    DrainOptions,
    TranscriptDrain,
    TranscriptSinks,
    build_transcript_callback,
)
from fakeyou.orchestration.turn_feed import EarlyCommit, TurnFeed
from fakeyou.persistence.record import MeetingRecord, TranscriptEntry, meeting_record_path
from fakeyou.session.contracts import ChatObserved
from fakeyou.session.kernel import SessionKernel
from tests._persistence_fakes import RecordingJournal

QUESTION = "Rani, when is the beta?"


class Conversation:
    def __init__(self, *, ready: bool = True) -> None:
        self.order: list[str] = []
        self.started: list[str] = []
        self.ready = ready
        self.finals: list[TranscriptEvent] = []
        self.interims: list[str] = []
        self.chats: list[TranscriptEvent] = []
        self.release = asyncio.Event()
        self.release.set()
        self.judging = asyncio.Event()
        self.interim_heard = asyncio.Event()

    async def handle_interim(self, text: str) -> None:
        self.interims.append(text)
        self.interim_heard.set()

    async def ready_to_answer(self, text: str) -> bool:
        del text
        return self.ready

    async def receive_final(self, event: TranscriptEvent) -> None:
        self.started.append(event.text)
        self.judging.set()
        await self.release.wait()
        self.finals.append(event)
        self.order.append(f"final:{event.text}")

    def receive_chat(self, event: TranscriptEvent) -> bool:
        self.chats.append(event)
        self.order.append(f"chat:{event.text}")
        return True

    async def withdraw(self, *, reason: str) -> None:
        del reason


class Setup:
    def __init__(
        self,
        tmp_path: Path,
        conversation: Conversation,
        *,
        hold: float | None = 0.0,
        per_stream: bool = False,
    ) -> None:
        self.conversation = conversation
        self.journal = RecordingJournal("meeting-9")
        self.record = MeetingRecord(meeting_record_path(tmp_path, SessionId("meeting-9")))
        self.kernel = KernelPort.for_meeting("meeting-9")
        self.kernel.call_state("in_call")
        self.moments: list[str] = []
        self.drain: TranscriptDrain = build_transcript_callback(
            sinks=TranscriptSinks(self.journal, self.record, kernel=self.kernel),
            binder=SlotBinder(),
            voice_activity=None,
            options=DrainOptions(per_stream_enabled=per_stream),
        )
        self.feed = TurnFeed(
            cast(Any, conversation),
            journal=self.journal,
            early=EarlyCommit(hold, self.drain.early_event) if hold is not None else None,
            on_final=self.observe_moment,
        )
        self.feed.attach(self.kernel)

    async def observe_moment(self, event: TranscriptEvent) -> None:
        self.moments.append(event.text)

    async def speak_then_pause(self, text: str) -> None:
        await self.drain.on_interim(TranscriptUpdate(text=text, final=False))
        await self.drain.on_vad(active=False)
        await self.kernel.settled()
        for _ in range(5):
            await asyncio.sleep(0)

    async def final(self, text: str, *, stream_id: str = "") -> None:
        await self.drain(
            TranscriptUpdate(
                text=text,
                final=True,
                timestamp="2026-10-25T01:00:00+00:00",
                stream_id=stream_id,
            )
        )

    async def close(self) -> None:
        await self.feed.close()
        self.record.close()


def recorded(record: MeetingRecord) -> list[str]:
    return [entry.text for entry in record.entries() if isinstance(entry, TranscriptEntry)]


async def test_a_finished_question_is_answered_before_the_final(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation())

    await setup.speak_then_pause(QUESTION)
    await setup.final(QUESTION)
    await setup.kernel.settled()

    assert [event.text for event in setup.conversation.finals] == [QUESTION]
    assert recorded(setup.record) == [QUESTION]
    assert [row["event"] for row in setup.journal.events("early_turn_verified")] == [
        "early_turn_verified"
    ]
    await setup.close()


async def test_a_final_swallowed_by_an_early_turn_still_reaches_the_moments(
    tmp_path: Path,
) -> None:
    setup = Setup(tmp_path, Conversation())

    await setup.speak_then_pause(QUESTION)
    await setup.final(QUESTION)
    await setup.kernel.settled()

    assert setup.moments == [QUESTION]
    await setup.close()


async def test_without_a_ready_judgement_the_final_starts_the_turn(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(ready=False))

    await setup.speak_then_pause("Rani, so about the")
    await setup.final("Rani, so about the beta?")
    await setup.kernel.settled()

    assert [event.text for event in setup.conversation.finals] == ["Rani, so about the beta?"]
    await setup.close()


async def test_early_turns_stay_off_without_a_hold(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None)

    await setup.speak_then_pause(QUESTION)

    assert setup.conversation.finals == []
    assert setup.conversation.interims == [QUESTION]
    await setup.close()


async def test_interims_keep_flowing_while_a_final_is_being_judged(tmp_path: Path) -> None:
    conversation = Conversation()
    conversation.release.clear()
    setup = Setup(tmp_path, conversation, hold=None)

    await setup.final(QUESTION)
    setup.kernel.drain()
    await asyncio.wait_for(conversation.judging.wait(), timeout=2)
    await setup.drain.on_interim(TranscriptUpdate(text="Wait, stop", final=False))
    setup.kernel.drain()
    await asyncio.wait_for(conversation.interim_heard.wait(), timeout=2)

    assert (conversation.interims, conversation.finals) == (["Wait, stop"], [])
    conversation.release.set()
    await setup.kernel.settled()
    assert [event.text for event in conversation.finals] == [QUESTION]
    await setup.close()


async def test_every_final_of_one_stream_window_is_judged(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None, per_stream=True)

    await setup.final("Rani, first?", stream_id="slot-0")
    await setup.final("Rani, second?", stream_id="slot-1")
    await setup.kernel.settled()

    assert [event.text for event in setup.conversation.finals] == ["Rani, first?", "Rani, second?"]
    await setup.close()


async def test_a_chat_line_reaches_the_conversation_through_the_kernel(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None)
    heard = TranscriptEvent(
        id="chat#1",
        timestamp="2026-10-25T01:00:00+00:00",
        source="meeting_chat",
        text="Rani, the link?",
        finalized=True,
        participant_id="dev-ayu",
        speaker_evidence_id=None,
    )

    setup.kernel.chat(
        ChatObserved(
            message_id="chat#1", device_id="dev-ayu", text=heard.text, own=False, backlog=False
        ),
        heard=heard,
    )
    await setup.kernel.settled()

    assert setup.conversation.chats == [heard]
    assert [line.text for line in setup.kernel.state.dialogue.lines] == ["Rani, the link?"]
    await setup.close()


async def test_a_failing_consumer_is_recorded_and_the_lane_keeps_going(tmp_path: Path) -> None:
    class Broken(Conversation):
        @override
        async def handle_interim(self, text: str) -> None:
            if text == "boom":
                raise RuntimeError("broken")
            await super().handle_interim(text)

    setup = Setup(tmp_path, Broken(), hold=None)

    await setup.drain.on_interim(TranscriptUpdate(text="boom", final=False))
    await setup.drain.on_interim(TranscriptUpdate(text="after", final=False))
    await setup.kernel.settled()

    assert setup.conversation.interims == ["after"]
    [fault] = setup.journal.events("fault_recorded")
    assert fault["detail"] == "turn_feed_voice: RuntimeError"
    await setup.close()


async def test_the_kernel_has_reduced_a_final_before_the_conversation_sees_it(
    tmp_path: Path,
) -> None:
    seen: list[int] = []
    setup = Setup(tmp_path, Conversation(), hold=None)
    kernel = setup.kernel

    class Watching(Conversation):
        @override
        async def receive_final(self, event: TranscriptEvent) -> None:
            seen.append(len(kernel.state.dialogue.lines))
            await super().receive_final(event)

    watching = TurnFeed(cast(Any, Watching()), journal=setup.journal)
    watching.attach(kernel)
    await setup.final(QUESTION)
    await kernel.settled()

    assert seen == [1]
    await watching.close()
    await setup.close()


def chat_event(text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=f"chat-{text}",
        timestamp="2026-10-25T01:00:00+00:00",
        source="meeting_chat",
        text=text,
        finalized=True,
        participant_id="dev-ayu",
        speaker_evidence_id=None,
    )


def offer_chat(kernel: KernelPort, text: str) -> None:
    kernel.chat(
        ChatObserved(
            message_id=f"chat-{text}", device_id="dev-ayu", text=text, own=False, backlog=False
        ),
        heard=chat_event(text),
    )


async def test_an_early_turn_waits_for_the_final_still_being_judged(tmp_path: Path) -> None:
    conversation = Conversation()
    conversation.release.clear()
    setup = Setup(tmp_path, conversation)

    await setup.final("Rani, first?")
    setup.kernel.drain()
    await asyncio.wait_for(conversation.judging.wait(), timeout=2)
    await setup.drain.on_interim(TranscriptUpdate(text="Rani, second?", final=False))
    await setup.drain.on_vad(active=False)
    setup.kernel.drain()
    await asyncio.wait_for(conversation.interim_heard.wait(), timeout=2)
    for _ in range(20):
        await asyncio.sleep(0)

    assert setup.journal.events("early_turn_started")
    assert (conversation.started, conversation.finals) == (["Rani, first?"], [])
    conversation.release.set()
    await asyncio.wait_for(setup.kernel.settled(), timeout=2)
    assert [event.text for event in conversation.finals] == ["Rani, first?", "Rani, second?"]
    await setup.close()


async def test_a_chat_after_a_final_reaches_the_turns_after_it(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None)

    await setup.final(QUESTION)
    offer_chat(setup.kernel, "Rani, the link?")
    await setup.kernel.settled()

    assert setup.conversation.order == [f"final:{QUESTION}", "chat:Rani, the link?"]
    await setup.close()


async def test_a_lane_survives_work_that_was_cancelled_under_it(tmp_path: Path) -> None:
    class Cancelling(Conversation):
        @override
        async def handle_interim(self, text: str) -> None:
            if text == "cancelled":
                raise asyncio.CancelledError
            await super().handle_interim(text)

    setup = Setup(tmp_path, Cancelling(), hold=None)

    await setup.drain.on_interim(TranscriptUpdate(text="cancelled", final=False))
    await setup.drain.on_interim(TranscriptUpdate(text="after", final=False))
    await asyncio.wait_for(setup.kernel.settled(), timeout=2)

    assert setup.conversation.interims == ["after"]
    [fault] = setup.journal.events("fault_recorded")
    assert fault["detail"] == "turn_feed_voice: CancelledError"
    await setup.close()


async def test_a_closed_feed_takes_no_more_work(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None)
    await setup.feed.close()

    await setup.drain.on_interim(TranscriptUpdate(text="late", final=False))
    await asyncio.wait_for(setup.kernel.settled(), timeout=2)

    assert setup.conversation.interims == []
    setup.record.close()


async def test_a_failing_turn_consumer_does_not_stop_the_kernel(tmp_path: Path) -> None:
    class Exploding:
        def accept(self, signal: TurnSignal) -> None:
            raise RuntimeError(f"cannot take {type(signal).__name__}")

        async def idle(self) -> None:
            return None

    setup = Setup(tmp_path, Conversation(), hold=None)
    setup.kernel.on_turn(Exploding())

    await setup.final(QUESTION)
    await setup.kernel.settled()

    assert [event.text for event in setup.conversation.finals] == [QUESTION]
    await setup.close()


async def test_the_turns_hear_nothing_once_the_call_has_ended(tmp_path: Path) -> None:
    setup = Setup(tmp_path, Conversation(), hold=None)
    setup.kernel.call_state("left")

    await setup.final(QUESTION)
    await setup.kernel.settled()

    assert setup.conversation.finals == []
    await setup.close()


async def test_payloads_the_kernel_never_reached_are_journaled_when_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kernel_port, "TURN_PAYLOAD_LIMIT", 2)
    journal = RecordingJournal("meeting-9")
    kernel = KernelPort.for_meeting("meeting-9", journal=journal)
    kernel.on_turn(cast(Any, SimpleNamespace(accept=lambda _signal: None)))

    for text in ("one", "two", "three"):
        kernel.interim(text)

    [evicted] = journal.events("turn_payload_dropped")
    assert evicted["kind"] == "HeardInterim"


async def test_a_dead_kernel_is_recorded_as_a_fault() -> None:
    journal = RecordingJournal("meeting-9")
    session = SessionKernel(session_id="meeting-9", attempt_id="attempt-1")
    kernel = KernelPort(session, journal=journal)

    def explode(_state: object, _event: object) -> None:
        raise RuntimeError("reducer bug")

    async def run() -> None:
        async with kernel.running():
            kernel.interim("anything")
            await asyncio.sleep(0.01)

    session.subscribe(explode)
    with pytest.raises(RuntimeError, match="reducer bug"):
        await run()
    await asyncio.sleep(0)

    [fault] = journal.events("fault_recorded")
    assert fault["detail"] == "session_kernel: RuntimeError"
