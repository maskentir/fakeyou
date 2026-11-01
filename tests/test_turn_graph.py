import asyncio
from collections.abc import AsyncIterator
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    TurnInput,
    TurnRuntime,
    _chat_reading,
    build_turn_graph,
)
from fakeyou.conversation.turn_judge import JudgeCache, TurnJudge, WindowSource
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.transcript import TranscriptEvent
from tests._conversation_fakes import ScriptedJudge, judged
from tests._prepared_support import PreparedSpeaker

TWIN_REPLY = "Yes, I am here."
QUESTION = "twin, are you there?"


class FakeResponder:
    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        self.transcripts.append(transcript)
        yield SpokenChunk(text=TWIN_REPLY, language=DEFAULTS.speech.language)


class FakeSpeaker(PreparedSpeaker):
    def __init__(self) -> None:
        self.utterances: list[str] = []
        self.is_speaking = False

    @override
    async def speak(self, utterance: str, **_: Any) -> None:
        self.utterances.append(utterance)

    async def stop_playback(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class FakeJournal:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.fields: list[dict[str, Any]] = []

    def record(self, event: str, *, source: str, **fields: Any) -> None:
        self.names.append(event)
        self.fields.append({"source": source, **fields})


def judging(judge: TurnJudge) -> TurnJudging:
    return TurnJudging(
        cache=JudgeCache(judge),
        windows=WindowSource(NoHistory(), window_lines=8),
        thresholds=JudgeThresholds(reply=0.6, clarify=0.5),
    )


def transcript(text: str = QUESTION) -> TranscriptEvent:
    return TranscriptEvent(
        id="e1",
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="Ana",
        speaker_evidence_id=None,
    )


def turn_input(*, addressed: bool = False, others_speaking: bool = False) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="final", event_id="e1", at=1.0, payload=QUESTION),
        context=ParticipationContext(addressed=addressed, others_speaking=others_speaking),
        speaking_generation=4,
        transcript_event=transcript(),
    )


def interim_input(text: str) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="interim", event_id="e0", at=0.5, payload=text),
        context=ParticipationContext(addressed=False),
        speaking_generation=4,
    )


def stop_input() -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="stop", event_id="s1", at=2.0, payload=""),
        context=ParticipationContext(addressed=False),
        speaking_generation=4,
    )


def runtime(
    speaker: FakeSpeaker | None = None,
    *,
    judge: TurnJudge | None = None,
    journal: FakeJournal | None = None,
    responder: FakeResponder | None = None,
) -> TurnRuntime:
    return TurnRuntime(
        speaker=speaker or FakeSpeaker(),
        journal=journal or FakeJournal(),
        responder=responder or FakeResponder(),
        judging=judging(judge) if judge is not None else None,
    )


async def test_the_final_reuses_the_judgement_started_on_its_interim() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"), delay=0.01)
    speaker = FakeSpeaker()
    graph = build_turn_graph()
    context = runtime(speaker, judge=judge)

    await graph.ainvoke(interim_input("twin are you there"), context=context)
    await graph.ainvoke(turn_input(), context=context)

    assert [window.latest.text for window in judge.windows] == ["twin are you there"]
    assert speaker.utterances == [TWIN_REPLY]


async def test_a_chat_turn_gets_the_same_typed_plan_as_speech() -> None:
    judge = ScriptedJudge(
        lambda _window: judged(
            "reply",
            meet_action="react",
            evidence=("screen",),
            reaction_emoji="\U0001f914",
        )
    )
    event = TranscriptEvent(
        id="c1",
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_chat",
        text="Harun, give me a confused reaction",
        finalized=True,
        participant_id="Ana",
        speaker_evidence_id=None,
    )

    reading = await _chat_reading(runtime(judge=judge), event)

    assert reading.meet_action == "react"
    assert reading.evidence == ("screen",)
    assert reading.reaction_emoji == "\U0001f914"


async def test_a_judge_that_stays_silent_never_reaches_the_responder() -> None:
    speaker = FakeSpeaker()
    responder = FakeResponder()

    await build_turn_graph().ainvoke(
        turn_input(),
        context=runtime(
            speaker, judge=ScriptedJudge(lambda _window: judged("stay_silent")), responder=responder
        ),
    )

    assert responder.transcripts == []
    assert speaker.utterances == []


async def test_a_judge_that_replies_speaks_exactly_once() -> None:
    speaker = FakeSpeaker()
    journal = FakeJournal()

    await build_turn_graph().ainvoke(
        turn_input(),
        context=runtime(
            speaker, judge=ScriptedJudge(lambda _window: judged("reply")), journal=journal
        ),
    )

    assert speaker.utterances == [TWIN_REPLY]
    decided = journal.fields[journal.names.index("policy_decided")]
    assert (decided["decision_source"], decided["reason"], decided["addressee"]) == (
        "judge",
        "judge_reply",
        "twin",
    )


async def test_a_failed_judge_falls_back_to_the_name_without_failing_the_turn() -> None:
    journal = FakeJournal()
    speaker = FakeSpeaker()

    out = await build_turn_graph().ainvoke(
        turn_input(addressed=True),
        context=runtime(speaker, judge=ScriptedJudge(fail=TimeoutError()), journal=journal),
    )

    assert out["failure"] is None
    assert speaker.utterances == [TWIN_REPLY]
    assert journal.names[:2] == ["policy_judge_failed", "policy_decided"]
    assert journal.fields[0]["code"] == "judge_timeout"
    assert journal.fields[1]["reason"] == "fallback_named"


async def test_without_a_judge_an_unnamed_line_stays_silent() -> None:
    speaker = FakeSpeaker()

    await build_turn_graph().ainvoke(turn_input(), context=runtime(speaker))

    assert speaker.utterances == []


async def test_an_occupied_floor_is_held_without_asking_the_judge() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    speaker = FakeSpeaker()

    await build_turn_graph().ainvoke(
        turn_input(addressed=True, others_speaking=True), context=runtime(speaker, judge=judge)
    )

    assert judge.windows == []
    assert speaker.utterances == []


async def test_stop_event_skips_the_judge_and_never_invokes_the_speaker() -> None:
    judge = ScriptedJudge(lambda _window: judged("reply"))
    speaker = FakeSpeaker()

    out = await build_turn_graph().ainvoke(stop_input(), context=runtime(speaker, judge=judge))

    assert judge.windows == []
    assert speaker.utterances == []
    assert out["speaking_generation"] == 5


async def test_speaking_generation_is_written_only_by_the_speak_node() -> None:
    graph = build_turn_graph()
    writers: list[str] = []

    for model_input in (stop_input(), turn_input(addressed=True)):
        async for chunk in graph.astream(model_input, context=runtime(), stream_mode="updates"):
            writers.extend(
                node for node, update in chunk.items() if "speaking_generation" in update
            )

    assert writers == ["speak", "speak"]
