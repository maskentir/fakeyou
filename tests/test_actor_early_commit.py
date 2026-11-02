import asyncio
from collections.abc import Callable, Sequence
from typing import override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.actor import EarlyOptions, says_more
from fakeyou.conversation.meeting_turns import MeetingTurns, TurnInteraction, build_meeting_turns
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.diagnostics import Diagnostics
from tests._audio_fakes import FakeReplyAudio
from tests._conversation_fakes import FakeResponder, ScriptedJudge, judged
from tests._conversation_support import event
from tests._persistence_fakes import RecordingJournal


def constant(verdict: TurnJudgement) -> Callable[[JudgeWindow], TurnJudgement]:
    def judge(window: JudgeWindow) -> TurnJudgement:
        del window
        return verdict

    return judge


async def test_a_finished_reply_judgement_is_ready_to_answer(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(
            twin_names=["Rani"], judge=ScriptedJudge(lambda _window: judged("reply"))
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.actor.bind(FakeReplyAudio())

    assert await loop.actor.ready_to_answer("Rani, when is the beta?") is True
    await loop.actor.close()


async def test_an_unfinished_or_silent_judgement_is_not_ready(journal: RecordingJournal) -> None:
    for verdict in (judged("reply", finished=False), judged("stay_silent")):
        loop = build_meeting_turns(
            FakeResponder(),
            interaction=TurnInteraction(
                twin_names=["Rani"], judge=ScriptedJudge(constant(verdict))
            ),
            diagnostics=Diagnostics(journal=journal),
        )
        loop.actor.bind(FakeReplyAudio())

        assert await loop.actor.ready_to_answer("Rani, so about the") is False
        await loop.actor.close()


async def test_without_a_judge_nothing_is_ready(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(twin_names=["Rani"]),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.actor.bind(FakeReplyAudio())

    assert await loop.actor.ready_to_answer("Rani, when is the beta?") is False
    await loop.actor.close()


QUESTION = "Rani, when is the beta?"
MORE = "Rani, when is the beta and who runs the QA for it"


def early_loop(
    journal: RecordingJournal,
    responder: FakeResponder,
    *,
    audio: FakeReplyAudio | None = None,
) -> tuple[MeetingTurns, FakeReplyAudio]:
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(
            twin_names=["Rani"], judge=ScriptedJudge(lambda _window: judged("reply"))
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = audio or FakeReplyAudio()
    loop.actor.bind(audio)
    counter = iter(range(100))
    loop.actor.configure_early(
        EarlyOptions(
            hold_seconds=0.0,
            build_event=lambda text: event(f"early-{next(counter)}", text),
        )
    )
    return loop, audio


class GatedSecondResponder(FakeResponder):
    def __init__(self) -> None:
        super().__init__()
        self.second_started = asyncio.Event()
        self.second_gate = asyncio.Event()

    @override
    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        if any("second" in item.text for item in events):
            self.second_started.set()
            await self.second_gate.wait()
            return SpokenChunk(text="Second answer.", language=DEFAULTS.speech.language)
        return await super().respond(events, on_tool_start)


async def test_a_finished_question_starts_the_turn_at_silence(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())

    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [QUESTION]
    assert journal.events("early_turn_started")
    await loop.actor.close()


async def test_the_final_of_an_early_turn_does_not_start_a_second_turn(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    absorbed = await loop.actor.settle_early(event("final-1", QUESTION))

    assert absorbed is True
    [verified] = journal.events("early_turn_verified")
    assert (verified["transcript_id"], verified["early_transcript_id"]) == ("final-1", "early-0")
    assert audio.spoken == [QUESTION]
    await loop.actor.close()


async def test_speaking_on_after_the_start_withdraws_the_early_turn(
    journal: RecordingJournal,
) -> None:
    responder = FakeResponder(asyncio.Event())
    loop, audio = early_loop(journal, responder)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    assert await asyncio.wait_for(responder.started.wait(), 2)

    await loop.actor.note_interim(MORE)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == []
    assert journal.events("early_turn_cancelled")[0]["reason"] == "speech_resumed"
    assert [row["reason"] for row in journal.events("agent_cancelled")] == ["speech_resumed"]
    assert await loop.actor.settle_early(event("final-1", MORE)) is False
    await loop.actor.close()


async def test_a_resumed_line_does_not_withdraw_a_later_turn(journal: RecordingJournal) -> None:
    responder = GatedSecondResponder()
    loop, audio = early_loop(journal, responder)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    await loop.actor.receive_final(event("t2", "Rani, second question?"))
    assert await asyncio.wait_for(responder.second_started.wait(), 2)
    await loop.actor.note_interim(MORE)
    responder.second_gate.set()
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert journal.events("early_turn_cancelled")
    assert journal.events("agent_cancelled") == []
    assert audio.spoken == [QUESTION, "Second answer."]
    await loop.actor.close()


class FirstBlockingAudio(FakeReplyAudio):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        if text == "Rani, first?":
            self.speaking = True
            self.entered.set()
            await self.release.wait()
            self.speaking = False
        await super().speak(
            text, sentence_index=sentence_index, transcript_id=transcript_id, language=language
        )


async def test_a_resumed_line_withdraws_a_queued_early_turn(journal: RecordingJournal) -> None:
    audio = FirstBlockingAudio()
    loop, _bound = early_loop(journal, FakeResponder(), audio=audio)
    await loop.actor.receive_final(event("t1", "Rani, first?"))
    assert await asyncio.wait_for(audio.entered.wait(), 2)

    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    for _ in range(200):
        if loop.actor.queue.pending:
            break
        await asyncio.sleep(0)
    assert loop.actor.queue.pending
    assert journal.events("early_turn_started")

    await loop.actor.note_interim(MORE)
    audio.release.set()
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert journal.events("early_turn_cancelled")[0]["reason"] == "speech_resumed"
    assert audio.spoken == []
    await loop.actor.close()


async def test_an_unfinished_line_waits_for_the_final(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(
            twin_names=["Rani"], judge=ScriptedJudge(lambda _window: judged("stay_silent"))
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.actor.bind(audio)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )

    await loop.actor.note_interim("Rani, so about the")
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == []
    assert await loop.actor.settle_early(event("final-1", "Rani, so about the beta?")) is False
    await loop.actor.close()


async def test_a_final_that_says_much_more_withdraws_the_early_turn(
    journal: RecordingJournal,
) -> None:
    responder = FakeResponder(asyncio.Event())
    loop, audio = early_loop(journal, responder)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    assert await asyncio.wait_for(responder.started.wait(), 2)

    absorbed = await loop.actor.settle_early(
        event("final-1", "Rani, when is the beta and who is running QA this time")
    )
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert absorbed is False
    assert journal.events("early_turn_cancelled")[0]["reason"] == "final_says_more"
    assert audio.spoken == []
    await loop.actor.close()


async def test_the_next_speaker_does_not_withdraw_the_early_turn(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    await loop.actor.note_interim("Budi, can you share the deck")

    assert journal.events("early_turn_cancelled") == []
    assert audio.spoken == [QUESTION]
    await loop.actor.close()


async def test_speech_before_the_hold_ends_starts_nothing(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(
            twin_names=["Rani"],
            judge=ScriptedJudge(lambda _window: judged("reply"), delay=0.05),
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.actor.bind(audio)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )

    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0)
    loop.actor.note_speech(active=True)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == []
    assert journal.events("early_turn_started") == []
    await loop.actor.close()


async def test_close_cancels_a_pending_early_hold(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=60.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0)

    await loop.actor.close()
    await asyncio.sleep(0)

    assert audio.spoken == []
    assert journal.events("early_turn_started") == []


def test_saying_more_means_the_line_grew_from_the_same_words() -> None:
    assert says_more("Rani, when is the beta and the launch", QUESTION)
    assert not says_more("Budi, can you share the deck", QUESTION)
    assert not says_more(QUESTION, QUESTION)
