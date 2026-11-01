import asyncio
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.actor import ActorWiring, FloorActor, IngressOptions
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
        self.is_speaking = True

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


async def test_a_short_line_never_takes_the_floor() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply", addressee="twin"))
    ingress, lifecycle, _, journal = build(judge)
    speech = await hold_the_floor(lifecycle)

    await ingress.handle_interim("twin hello")
    await settle(lifecycle, ingress)

    assert journal.of("barge_in_detected") == []
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


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
