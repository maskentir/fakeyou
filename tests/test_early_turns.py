import asyncio

from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.orchestration.early_turns import EarlyTurns, says_more
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal

QUESTION = "Rani, when is the beta?"


class FakeAnswering:
    def __init__(self, *, ready: bool = True, gate: asyncio.Event | None = None) -> None:
        self.ready = ready
        self.gate = gate
        self.asked: list[str] = []
        self.started: list[TranscriptEvent] = []
        self.withdrawn: list[str] = []

    async def ready_to_answer(self, text: str) -> bool:
        self.asked.append(text)
        if self.gate is not None:
            await self.gate.wait()
        return self.ready

    async def receive_final(self, event: TranscriptEvent) -> None:
        self.started.append(event)
        await asyncio.sleep(0)

    async def withdraw(self, *, reason: str) -> None:
        self.withdrawn.append(reason)


def turns(answering: FakeAnswering, journal: RecordingJournal) -> EarlyTurns:
    counter = iter(range(100))
    return EarlyTurns(
        answering,
        hold_seconds=0.0,
        build_event=lambda text: event(f"early-{next(counter)}", text),
        journal=journal,
    )


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_finished_question_starts_the_turn_at_silence(journal: RecordingJournal) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)

    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    assert [started.text for started in answering.started] == [QUESTION]
    assert journal.events("early_turn_started")


async def test_the_final_of_an_early_turn_does_not_start_a_second_turn(
    journal: RecordingJournal,
) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    absorbed = await early.settle(event("final-1", "Rani, when is the beta?"))

    assert absorbed is True
    [verified] = journal.events("early_turn_verified")
    assert (verified["transcript_id"], verified["early_transcript_id"]) == ("final-1", "early-0")


async def test_an_unfinished_line_waits_for_the_final(journal: RecordingJournal) -> None:
    answering = FakeAnswering(ready=False)
    early = turns(answering, journal)

    await early.on_interim("Rani, so about the")
    early.on_speech(active=False)
    await settle()

    assert answering.started == []
    assert await early.settle(event("final-1", "Rani, so about the beta?")) is False


async def test_speech_before_the_hold_ends_starts_nothing(journal: RecordingJournal) -> None:
    gate = asyncio.Event()
    answering = FakeAnswering(gate=gate)
    early = turns(answering, journal)

    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()
    early.on_speech(active=True)
    gate.set()
    await settle()

    assert answering.started == []


async def test_speaking_on_after_the_start_withdraws_the_turn(journal: RecordingJournal) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    await early.on_interim("Rani, when is the beta and who runs the QA for it")

    assert answering.withdrawn == ["speech_resumed"]
    assert await early.settle(event("final-1", "Rani, when is the beta and who runs QA?")) is False
    assert journal.events("early_turn_cancelled")[0]["reason"] == "speech_resumed"


async def test_a_final_that_says_much_more_withdraws_the_turn(journal: RecordingJournal) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    absorbed = await early.settle(
        event("final-1", "Rani, when is the beta and who is running QA this time")
    )

    assert absorbed is False
    assert answering.withdrawn == ["final_says_more"]


async def test_the_next_speaker_does_not_withdraw_the_turn(journal: RecordingJournal) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    await early.on_interim("Budi, can you share the deck")

    assert answering.withdrawn == []


async def test_a_new_line_while_the_turn_starts_does_not_cancel_it(
    journal: RecordingJournal,
) -> None:
    answering = FakeAnswering()
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await early.on_interim("Budi, can you share the deck")
    await settle()

    assert [started.text for started in answering.started] == [QUESTION]


async def test_close_cancels_a_pending_hold(journal: RecordingJournal) -> None:
    gate = asyncio.Event()
    answering = FakeAnswering(gate=gate)
    early = turns(answering, journal)
    await early.on_interim(QUESTION)
    early.on_speech(active=False)
    await settle()

    await early.close()
    gate.set()
    await settle()

    assert answering.started == []


def test_saying_more_means_the_line_grew_from_the_same_words() -> None:
    assert says_more("Rani, when is the beta and the launch", QUESTION)
    assert not says_more("Budi, can you share the deck", QUESTION)
    assert not says_more(QUESTION, QUESTION)
