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
    prosody_expected: bool = False,
    judge: ScriptedJudge | None = None,
) -> tuple[MeetingTurns, FakeReplyAudio]:
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(
            twin_names=["Rani"],
            judge=judge or ScriptedJudge(lambda _window: judged("reply")),
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
            prosody_expected=prosody_expected,
            prosody_wait_seconds=0.05,
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


async def test_a_declined_early_commit_is_journaled_with_its_reason(
    journal: RecordingJournal,
) -> None:
    cases = (
        (judged("reply", finished=False), "judge_unfinished"),
        (judged("stay_silent"), "judge_not_admitted"),
    )
    for verdict, reason in cases:
        loop = build_meeting_turns(
            FakeResponder(),
            interaction=TurnInteraction(
                twin_names=["Rani"], judge=ScriptedJudge(constant(verdict))
            ),
            diagnostics=Diagnostics(journal=journal),
        )
        loop.actor.bind(FakeReplyAudio())
        loop.actor.configure_early(
            EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
        )

        await loop.actor.note_interim("Rani, so about the")
        loop.actor.note_speech(active=False)
        await asyncio.wait_for(loop.actor.wait_idle(), 2)

        [declined] = journal.events("early_turn_declined")
        assert declined["reason"] == reason
        assert journal.events("early_turn_started") == []
        journal.lines.clear()
        await loop.actor.close()


async def test_a_hold_without_a_judge_is_journaled_as_declined(
    journal: RecordingJournal,
) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(twin_names=["Rani"]),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.actor.bind(FakeReplyAudio())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )

    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "no_judge"
    await loop.actor.close()


async def test_a_final_that_beats_the_hold_is_journaled_as_declined(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=60.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0)

    absorbed = await loop.actor.settle_early(event("final-1", QUESTION))

    assert absorbed is False
    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "final_first"
    assert audio.spoken == []
    await loop.actor.close()


async def test_speech_resuming_during_the_hold_is_journaled_as_superseded(
    journal: RecordingJournal,
) -> None:
    loop, _audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=60.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0)

    loop.actor.note_speech(active=True)

    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "superseded"
    await loop.actor.close()


async def test_an_interim_arriving_after_the_stop_still_commits(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.05, build_event=lambda text: event("early-0", text))
    )

    loop.actor.note_speech(active=False)
    await loop.actor.note_interim(QUESTION)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [QUESTION]
    assert journal.events("early_turn_started")
    assert journal.events("early_turn_declined") == []
    await loop.actor.close()


async def test_text_growth_during_the_hold_restarts_stability_and_commits_the_grown_text(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.08, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0.03)

    await loop.actor.note_interim(MORE)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [MORE]
    assert journal.events("early_turn_started")
    assert journal.events("early_turn_declined") == []
    await loop.actor.close()


async def test_a_repeated_interim_does_not_restart_the_hold(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.2, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0.12)

    await loop.actor.note_interim(QUESTION)
    await asyncio.sleep(0.14)

    assert audio.spoken == [QUESTION]
    await loop.actor.close()


async def test_text_growth_while_the_judge_runs_commits_the_grown_text_once(
    journal: RecordingJournal,
) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(
            twin_names=["Rani"],
            judge=ScriptedJudge(lambda _window: judged("reply"), delay=0.15),
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
    await asyncio.sleep(0.03)

    await loop.actor.note_interim(MORE)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [MORE]
    assert len(journal.events("early_turn_started")) == 1
    assert journal.events("early_turn_declined") == []
    await loop.actor.close()


async def test_a_duplicate_stop_does_not_decline_the_same_text_twice(
    journal: RecordingJournal,
) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(
            twin_names=["Rani"], judge=ScriptedJudge(lambda _window: judged("stay_silent"))
        ),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.actor.bind(FakeReplyAudio())
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert len(journal.events("early_turn_declined")) == 1
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


async def test_the_commit_reuses_the_judgement_already_made_for_the_interim(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(twin_names=["Rani"], judge=judge),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.actor.bind(audio)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.handle_interim(QUESTION)
    await asyncio.sleep(0.05)
    calls = len(judge.windows)
    assert calls >= 1

    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [QUESTION]
    assert len(judge.windows) == calls
    await loop.actor.close()


async def test_interims_during_speech_do_not_start_judgements(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    loop, _audio = early_loop(journal, FakeResponder(), judge=judge)
    loop.actor.note_speech(active=True)
    await loop.actor.handle_interim(QUESTION)
    await asyncio.sleep(0.05)

    assert judge.windows == []

    loop.actor.note_speech(active=False)
    await loop.actor.handle_interim(QUESTION)
    await asyncio.sleep(0.05)

    assert len(judge.windows) == 1
    await loop.actor.close()


async def test_a_commit_after_speech_uses_the_arm_judgement_only(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.05)
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    loop.actor.note_speech(active=True)
    await loop.actor.handle_interim(QUESTION)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    assert audio.spoken == [QUESTION]
    assert len(judge.windows) == 1
    await loop.actor.close()


async def test_an_appended_revision_reuses_the_armed_judgement(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.3)
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    await loop.actor.note_interim(QUESTION)
    await loop.actor.note_interim("Rani, when is the beta launch")
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    assert audio.spoken == [QUESTION]
    assert [window.latest.text for window in judge.windows] == [QUESTION]
    await loop.actor.close()


async def test_a_declined_arm_judges_the_appended_text_next(
    journal: RecordingJournal,
) -> None:
    def verdict(window: JudgeWindow) -> TurnJudgement:
        if len(window.latest.text.split()) < 6:
            return judged("reply", finished=False)
        return judged("reply")

    judge = ScriptedJudge(verdict, delay=0.3)
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    await loop.actor.note_interim(QUESTION)
    await asyncio.sleep(0.05)
    await loop.actor.note_interim("Rani, when is the beta launch")
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    assert audio.spoken == ["Rani, when is the beta launch"]
    assert len(judge.windows) == 2
    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "judge_unfinished"
    await loop.actor.close()


async def test_a_trailing_revision_of_the_answered_line_keeps_the_answer(
    journal: RecordingJournal,
) -> None:
    gate = asyncio.Event()
    responder = FakeResponder(gate=gate)
    loop, audio = early_loop(journal, responder)
    loop.actor.configure_barge_in(enabled=True)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(responder.started.wait(), 2)

    await loop.actor.handle_interim("Rani, when is the beta start")
    await asyncio.sleep(0.1)

    assert not responder.cancelled.is_set()
    gate.set()
    await asyncio.wait_for(loop.actor.wait_idle(), 3)
    assert audio.spoken == [QUESTION]
    await loop.actor.close()


async def test_a_long_amendment_of_the_answered_line_still_takes_the_floor(
    journal: RecordingJournal,
) -> None:
    gate = asyncio.Event()
    responder = FakeResponder(gate=gate)
    loop, _audio = early_loop(journal, responder)
    loop.actor.configure_barge_in(enabled=True)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(responder.started.wait(), 2)

    await loop.actor.handle_interim("Rani, when is the beta and who runs the QA for it")
    await asyncio.sleep(0.1)

    assert responder.cancelled.is_set()
    gate.set()
    await loop.actor.close()


async def test_a_new_line_still_takes_the_floor_from_an_answer_in_flight(
    journal: RecordingJournal,
) -> None:
    gate = asyncio.Event()
    responder = FakeResponder(gate=gate)
    loop, _audio = early_loop(journal, responder)
    loop.actor.configure_barge_in(enabled=True)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(responder.started.wait(), 2)

    await loop.actor.handle_interim("Rani, forget that, what about the budget instead")
    await asyncio.sleep(0.1)

    assert responder.cancelled.is_set()
    gate.set()
    await loop.actor.close()


async def test_a_changed_line_asks_the_judge_again_instead_of_reusing(
    journal: RecordingJournal,
) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(twin_names=["Rani"], judge=judge),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.actor.bind(audio)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.handle_interim(QUESTION)
    await asyncio.sleep(0.05)
    calls = len(judge.windows)

    await loop.actor.note_interim(MORE)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [MORE]
    assert len(judge.windows) > calls
    await loop.actor.close()


async def test_the_judge_starts_when_the_hold_arms(journal: RecordingJournal) -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.5)
    loop, _audio = early_loop(journal, FakeResponder(), judge=judge)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=5.0, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)

    for _ in range(50):
        if judge.windows:
            break
        await asyncio.sleep(0.01)

    assert [window.latest.text for window in judge.windows] == [QUESTION]
    await loop.actor.close()


class TimingJudge(ScriptedJudge):
    def __init__(self, *, delay: float = 0.0, fail: BaseException | None = None) -> None:
        super().__init__(lambda _window: judged("reply"), delay=delay, fail=fail)
        self.started_at: list[float] = []

    @override
    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        self.started_at.append(asyncio.get_running_loop().time())
        return await super().judge(window)


async def test_the_commit_waits_only_for_the_judgement_started_at_arm(
    journal: RecordingJournal,
) -> None:
    judge = TimingJudge(delay=0.5)
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.4, build_event=lambda text: event("early-0", text))
    )
    armed_at = asyncio.get_running_loop().time()
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    assert audio.spoken == [QUESTION]
    assert len(judge.windows) == 1
    assert judge.started_at[0] - armed_at < 0.2
    await loop.actor.close()


async def test_a_resumed_arm_shares_one_judgement_with_the_next_stop(
    journal: RecordingJournal,
) -> None:
    judge = TimingJudge(delay=0.3)
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    loop.actor.configure_early(
        EarlyOptions(hold_seconds=0.1, build_event=lambda text: event("early-0", text))
    )
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.sleep(0.05)
    loop.actor.note_speech(active=True)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    assert audio.spoken == [QUESTION]
    assert len(judge.windows) == 1
    await loop.actor.close()


async def test_a_judge_failure_at_arm_declines_once(journal: RecordingJournal) -> None:
    judge = TimingJudge(fail=RuntimeError("judge down"))
    loop, audio = early_loop(journal, FakeResponder(), judge=judge)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 3)

    loop.actor.note_speech(active=False)
    await asyncio.sleep(0.05)

    assert audio.spoken == []
    assert len(judge.windows) == 1
    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "judge_failed"
    await loop.actor.close()


async def test_incomplete_prosody_blocks_the_commit(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    loop.actor.note_turn_end(probability=0.2)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == []
    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "prosody_incomplete"
    await loop.actor.close()


async def test_a_score_arriving_during_speech_is_ignored(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=True)
    loop.actor.note_turn_end(probability=0.1)
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [QUESTION]
    await loop.actor.close()


async def test_a_consumed_score_does_not_gate_the_next_utterance(
    journal: RecordingJournal,
) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    loop.actor.note_turn_end(probability=0.1)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)
    assert audio.spoken == []

    loop.actor.note_speech(active=True)
    await loop.actor.note_interim("Rani, and the timeline?")
    loop.actor.note_speech(active=False)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == ["Rani, and the timeline?"]
    await loop.actor.close()


async def test_readiness_waits_briefly_for_a_pending_score(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder(), prosody_expected=True)
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)

    async def late_score() -> None:
        await asyncio.sleep(0.01)
        loop.actor.note_turn_end(probability=0.1)

    task = asyncio.create_task(late_score())
    await asyncio.wait_for(loop.actor.wait_idle(), 2)
    await task

    assert audio.spoken == []
    [declined] = journal.events("early_turn_declined")
    assert declined["reason"] == "prosody_incomplete"
    await loop.actor.close()


async def test_complete_prosody_lets_the_commit_through(journal: RecordingJournal) -> None:
    loop, audio = early_loop(journal, FakeResponder())
    await loop.actor.note_interim(QUESTION)
    loop.actor.note_speech(active=False)
    loop.actor.note_turn_end(probability=0.9)
    await asyncio.wait_for(loop.actor.wait_idle(), 2)

    assert audio.spoken == [QUESTION]
    assert journal.events("early_turn_declined") == []
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
