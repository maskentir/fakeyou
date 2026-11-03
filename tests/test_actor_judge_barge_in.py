import asyncio
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.actor import ActorWiring, FloorActor, IngressOptions, IngressTiming
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.conversation.turn_judge import JudgeCache, WindowSource
from tests._conversation_fakes import ScriptedJudge, judged
from tests._prepared_support import PreparedSpeaker


class FakeSession(PreparedSpeaker):
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.ducks = 0
        self.unducks = 0
        self.freezes = 0
        self.thaws = 0
        self.is_speaking = True

    def duck(self) -> None:
        self.ducks += 1

    def unduck(self) -> None:
        self.unducks += 1

    def freeze(self) -> None:
        self.freezes += 1

    def thaw(self) -> None:
        self.thaws += 1

    @override
    async def speak(self, text: str, **_: Any) -> None:
        self.spoken.append(text)

    async def stop_playback(self) -> None:
        self.stops += 1

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class FakeJournal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.entries.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class FakeResponder:
    async def stream_reply(self, _transcript: str) -> Any:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


def judging(judge: ScriptedJudge) -> TurnJudging:
    return TurnJudging(
        cache=JudgeCache(judge),
        windows=WindowSource(NoHistory(), window_lines=8),
        thresholds=JudgeThresholds(reply=0.6, clarify=0.5, barge_in=0.6),
    )


def build(
    judge: ScriptedJudge,
    *,
    barge_in_judge_enabled: bool = True,
    duck_grace_seconds: float = 1.5,
    freeze_sustain_seconds: float = 0.6,
    freeze_gap_seconds: float = 0.25,
) -> tuple[FloorActor, FloorActor, FakeSession, FakeJournal]:
    journal = FakeJournal()
    lifecycle = FloorActor(
        build_turn_graph(),
        ActorWiring(
            journal=journal,
            responder=FakeResponder(),
            judging=judging(judge),
            ingress=IngressOptions(
                addressing_names=("twin",),
                timing=IngressTiming(
                    duck_grace_seconds=duck_grace_seconds,
                    freeze_sustain_seconds=freeze_sustain_seconds,
                    freeze_gap_seconds=freeze_gap_seconds,
                ),
                barge_in_judge_enabled=barge_in_judge_enabled,
            ),
        ),
    )
    session = FakeSession()
    lifecycle.bind(session)
    return lifecycle, lifecycle, session, journal


async def hold_the_floor(lifecycle: FloorActor) -> asyncio.Task[bool]:
    async def wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(lifecycle.floor.hold(wait_forever()))
    await asyncio.sleep(0)
    return task


async def settle(lifecycle: FloorActor, ingress: FloorActor) -> None:
    judging = lifecycle.judging
    assert judging is not None
    await judging.cache.wait_idle()
    await ingress.settled()


async def test_the_judge_stops_the_twin_when_the_line_is_for_it() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin, can you hear me")
    await settle(lifecycle, ingress)

    assert speech.done()
    assert journal.of("barge_in_detected") == [
        {"source": "agent", "trigger": "interim", "words": 5, "decided_by": "judge"}
    ]
    assert session.stops == 1


async def test_the_twin_keeps_the_floor_when_the_line_is_for_someone_else() -> None:
    judge = ScriptedJudge(lambda _window: judged("stay_silent", addressee="someone_else"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("ayu, can you hear me")
    await settle(lifecycle, ingress)

    assert not speech.done()
    assert journal.of("barge_in_detected") == []
    assert session.stops == 0
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_a_backchannel_does_not_take_the_floor() -> None:
    judge = ScriptedJudge(lambda _window: judged("stay_silent", addressee="twin", backchannel=True))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin, yes exactly right")
    await settle(lifecycle, ingress)

    assert not speech.done()
    assert journal.of("barge_in_detected") == []
    assert session.stops == 0
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_a_line_the_judge_is_not_sure_about_keeps_the_floor() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin", confidence=0.4))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin, can you hear me")
    await settle(lifecycle, ingress)

    assert not speech.done()
    assert journal.of("barge_in_detected") == []
    assert session.stops == 0
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_a_silent_judge_still_yields_the_floor() -> None:
    judge = ScriptedJudge(fail=ConnectionError("the judge is down"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin, can you hear me")
    await settle(lifecycle, ingress)

    assert speech.done()
    assert journal.of("barge_in_detected") == [
        {"source": "agent", "trigger": "interim", "words": 5, "decided_by": "judge_unavailable"}
    ]
    assert session.stops == 1


async def test_a_short_line_is_judged_like_any_other() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin hello")
    await settle(lifecycle, ingress)

    assert journal.of("barge_in_detected") == [
        {"source": "agent", "trigger": "interim", "words": 2, "decided_by": "judge"}
    ]
    assert speech.done()
    assert session.stops == 1


async def test_voice_onset_ducks_before_any_words_arrive() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)

    assert session.ducks == 1
    assert session.stops == 0
    assert not speech.done()
    assert journal.of("barge_in_ducked") == [{"source": "agent"}]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_sustained_voice_freezes_playback_while_ducked() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge, freeze_sustain_seconds=0.05)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.15)

    assert session.ducks == 1
    assert session.freezes == 1
    assert session.stops == 0
    assert not speech.done()
    assert journal.of("barge_in_frozen") == [{"source": "agent"}]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_a_word_gap_inside_a_sustained_interruption_still_freezes() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge, freeze_sustain_seconds=0.1)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.08)
    ingress.note_speech(active=False)
    await asyncio.sleep(0.05)
    ingress.note_speech(active=True)
    await asyncio.sleep(0.2)

    assert session.freezes == 1
    assert journal.of("barge_in_frozen") == [{"source": "agent"}]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_a_sustained_resumption_rearms_the_freeze() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(
        judge,
        duck_grace_seconds=2.0,
        freeze_sustain_seconds=0.10,
        freeze_gap_seconds=0.03,
    )
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.02)
    ingress.note_speech(active=False)
    await asyncio.sleep(0.22)
    assert session.freezes == 0

    ingress.note_speech(active=True)
    await asyncio.sleep(0.16)

    assert session.ducks == 1
    assert session.freezes == 1
    assert journal.of("barge_in_frozen") == [{"source": "agent"}]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_a_short_burst_never_freezes() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, _journal = build(
        judge, duck_grace_seconds=0.05, freeze_sustain_seconds=0.3
    )
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.02)
    ingress.note_speech(active=False)
    await asyncio.sleep(0.4)

    assert session.ducks == 1
    assert session.freezes == 0
    assert session.unducks == 1
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_a_rejected_barge_in_thaws_a_freeze() -> None:
    judge = ScriptedJudge(lambda _window: judged("stay_silent", addressee="someone_else"))
    ingress, lifecycle, session, journal = build(judge, freeze_sustain_seconds=0.02)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.08)
    assert session.freezes == 1
    await ingress.handle_interim("ayu, can you hear me")
    await settle(lifecycle, ingress)

    assert session.thaws == 1
    assert session.unducks == 1
    assert journal.of("barge_in_resumed") == [{"source": "agent", "reason": "judge"}]
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_pausing_lifts_the_duck_and_disarms_the_freeze() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, _journal = build(judge, freeze_sustain_seconds=0.05)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await lifecycle.pause()
    await asyncio.sleep(0.15)

    assert session.ducks == 1
    assert session.unducks == 1
    assert session.freezes == 0
    assert session.stops == 1
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_a_rejected_barge_in_lifts_the_duck() -> None:
    judge = ScriptedJudge(lambda _window: judged("stay_silent", addressee="someone_else"))
    ingress, lifecycle, session, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await ingress.handle_interim("ayu, can you hear me")
    await settle(lifecycle, ingress)

    assert session.ducks == 1
    assert session.unducks == 1
    assert journal.of("barge_in_resumed") == [{"source": "agent", "reason": "judge"}]
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


class GatedJudge(ScriptedJudge):
    def __init__(self) -> None:
        super().__init__()
        self.gates: dict[str, asyncio.Event] = {}

    def gate(self, text: str) -> asyncio.Event:
        event = asyncio.Event()
        self.gates[text] = event
        return event

    @override
    async def judge(self, window: Any) -> Any:
        self.windows.append(window)
        gate = self.gates.get(window.latest.text)
        if gate is not None:
            await gate.wait()
        if window.latest.text.startswith("twin"):
            return judged("reply", addressee="twin")
        return judged("stay_silent", addressee="someone_else")


async def test_a_slow_rejection_does_not_lift_a_newer_duck() -> None:
    judge = GatedJudge()
    first = judge.gate("ayu, can you hear me")
    second = judge.gate("twin, can you hear me")
    ingress, lifecycle, session, _journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await ingress.handle_interim("ayu, can you hear me")
    await ingress.handle_interim("twin, can you hear me")
    first.set()
    await asyncio.sleep(0.05)

    assert session.unducks == 0
    second.set()
    await settle(lifecycle, ingress)
    assert speech.done()
    await lifecycle.close()


async def test_close_lifts_the_duck() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, _journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await lifecycle.close()

    assert session.ducks == 1
    assert session.unducks == 1
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_ducking_stays_while_the_voice_is_still_active() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge, duck_grace_seconds=0.05)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    await asyncio.sleep(0.12)
    assert session.unducks == 0
    ingress.note_speech(active=False)
    await asyncio.sleep(0.12)

    assert session.unducks == 1
    assert journal.of("barge_in_resumed") == [{"source": "agent", "reason": "grace"}]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_ducking_lifts_itself_when_no_words_follow() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, session, journal = build(judge, duck_grace_seconds=0.05)
    speech = await hold_the_floor(lifecycle)

    ingress.note_speech(active=True)
    ingress.note_speech(active=False)
    await asyncio.sleep(0.2)

    assert session.ducks == 1
    assert session.unducks == 1
    assert journal.of("barge_in_resumed") == [{"source": "agent", "reason": "grace"}]
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
    await lifecycle.close()


async def test_the_word_count_barge_in_ignores_the_judge() -> None:
    judge = ScriptedJudge(lambda _window: judged("stay_silent", addressee="someone_else"))
    ingress, lifecycle, session, journal = build(judge, barge_in_judge_enabled=False)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("ayu, can you hear me")
    await settle(lifecycle, ingress)

    assert speech.done()
    assert journal.of("barge_in_detected") == [
        {"source": "agent", "trigger": "interim", "words": 5}
    ]
    assert session.stops == 1


async def test_one_interim_is_judged_once() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, _, _ = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin, can you hear me")
    await settle(lifecycle, ingress)

    assert [window.latest.text for window in judge.windows] == ["twin, can you hear me"]
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)
