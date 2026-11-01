import asyncio
from collections.abc import Callable

from fakeyou.conversation.meeting_turns import TurnInteraction, build_meeting_turns
from fakeyou.conversation.turn_judge import JudgeWindow, TurnJudgement
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
    loop.lifecycle.bind(FakeReplyAudio())

    assert await loop.ingress.ready_to_answer("Rani, when is the beta?") is True
    await loop.lifecycle.close()


async def test_an_unfinished_or_silent_judgement_is_not_ready(journal: RecordingJournal) -> None:
    for verdict in (judged("reply", finished=False), judged("stay_silent")):
        loop = build_meeting_turns(
            FakeResponder(),
            interaction=TurnInteraction(
                twin_names=["Rani"], judge=ScriptedJudge(constant(verdict))
            ),
            diagnostics=Diagnostics(journal=journal),
        )
        loop.lifecycle.bind(FakeReplyAudio())

        assert await loop.ingress.ready_to_answer("Rani, so about the") is False
        await loop.lifecycle.close()


async def test_without_a_judge_nothing_is_ready(journal: RecordingJournal) -> None:
    loop = build_meeting_turns(
        FakeResponder(),
        interaction=TurnInteraction(twin_names=["Rani"]),
        diagnostics=Diagnostics(journal=journal),
    )
    loop.lifecycle.bind(FakeReplyAudio())

    assert await loop.ingress.ready_to_answer("Rani, when is the beta?") is False
    await loop.lifecycle.close()


async def test_withdraw_cancels_the_turn_in_flight(journal: RecordingJournal) -> None:
    responder = FakeResponder(asyncio.Event())
    loop = build_meeting_turns(
        responder,
        interaction=TurnInteraction(twin_names=["Rani"]),
        diagnostics=Diagnostics(journal=journal),
    )
    audio = FakeReplyAudio()
    loop.lifecycle.bind(audio)

    await loop.ingress.receive_final(event("t1", "Rani, when is the beta?"))
    assert await asyncio.wait_for(responder.started.wait(), 2)

    await loop.ingress.withdraw(reason="speech_resumed")
    await asyncio.wait_for(loop.lifecycle.wait_idle(), 2)

    assert audio.spoken == []
    assert [row["reason"] for row in journal.events("agent_cancelled")] == ["speech_resumed"]
    await loop.lifecycle.close()
