import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.jev_refresh import JevRefreshInput, JevState
from fakeyou.conversation.participation_decider import (
    DeciderTurn,
    ParticipationDecision,
)
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import TurnInput, TurnRuntime, build_turn_graph
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.transcript import TranscriptEvent

TWIN_REPLY = "Yes, I am here."


class FakeResponder:
    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        self.transcripts.append(transcript)
        yield SpokenChunk(text=TWIN_REPLY, language=DEFAULTS.speech.language)


class FakeDecider:
    def __init__(
        self, decision: ParticipationDecision | None, error: Exception | None = None
    ) -> None:
        self._decision = decision
        self._error = error
        self.windows: list[tuple[DeciderTurn, ...]] = []

    async def decide(self, window: Sequence[DeciderTurn]) -> ParticipationDecision:
        self.windows.append(tuple(window))
        if self._error is not None:
            raise self._error
        assert self._decision is not None
        return self._decision


class FakeSpeaker:
    def __init__(self) -> None:
        self.utterances: list[str] = []
        self.is_speaking = False

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


def final_event(payload: str = "twin, are you there?") -> TurnEvent:
    return TurnEvent(kind="final", event_id="e1", at=1.0, payload=payload)


def mentioned_context() -> ParticipationContext:
    return ParticipationContext(
        addressed=False,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=False,
        turn_cue="explicit",
        turn_mine=True,
    )


def addressed_context() -> ParticipationContext:
    return ParticipationContext(
        addressed=True,
        followup=False,
        same_participant=False,
        ambiguous=False,
        others_speaking=False,
        activation_valid=True,
    )


def turn_input(context: ParticipationContext) -> TurnInput:
    return TurnInput(
        event=final_event(),
        turns=[DeciderTurn(speaker="Ana", text="twin, are you there?", mine=True)],
        context=context,
        speaking_generation=4,
    )


def stop_input() -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="stop", event_id="s1", at=2.0, payload=""),
        turns=[],
        context=mentioned_context(),
        speaking_generation=4,
    )


def decision(kind: Literal["speak", "silent", "clarify"]) -> ParticipationDecision:
    return ParticipationDecision(decision=kind, confidence=0.9, reason_code="model_says")


async def seeded_jev(decider: FakeDecider) -> JevState:
    jev = JevState()
    await jev.cache.refresh(
        decider,
        JevRefreshInput(
            event_ids=(),
            turns=(DeciderTurn(speaker="Ana", text="twin, are you there?", mine=True),),
            cue="explicit",
            mine=True,
            window_turns=6,
        ),
    )
    return jev


async def test_mentioned_turn_reaches_the_decider_only_through_the_interim_refresh() -> None:
    decider = FakeDecider(decision("silent"))
    jev = JevState()
    graph = build_turn_graph(decider=decider, window_turns=6)
    runtime = TurnRuntime(
        speaker=FakeSpeaker(), journal=FakeJournal(), responder=FakeResponder(), jev=jev
    )
    history = [
        TranscriptEvent(
            id="e1",
            timestamp="2026-10-17T10:00:00+00:00",
            source="meeting_audio",
            text="twin, are you there?",
            finalized=True,
            participant_id="Ana",
            speaker_evidence_id=None,
        )
    ]

    await graph.ainvoke(turn_input(mentioned_context()), context=runtime)
    assert decider.windows == []

    await graph.ainvoke(
        TurnInput(
            event=TurnEvent(kind="interim", event_id="e1", at=2.0, payload="twin, are you there"),
            turns=[],
            context=mentioned_context(),
            speaking_generation=4,
            history=history,
            addressing_names=("twin",),
        ),
        context=runtime,
    )
    await jev.wait_idle()

    assert len(decider.windows) == 1
    assert decider.windows[0][-1]["text"] == "twin, are you there?"


async def test_addressed_turn_is_answered_on_rules_without_consulting_decider() -> None:
    decider = FakeDecider(None)
    speaker = FakeSpeaker()
    graph = build_turn_graph(decider=decider, window_turns=6)

    await graph.ainvoke(
        turn_input(addressed_context()),
        context=TurnRuntime(speaker=speaker, journal=FakeJournal(), responder=FakeResponder()),
    )

    assert decider.windows == []
    assert speaker.utterances == [TWIN_REPLY]


async def test_silent_decision_does_not_invoke_the_speaker() -> None:
    speaker = FakeSpeaker()
    graph = build_turn_graph(decider=FakeDecider(decision("silent")), window_turns=6)

    await graph.ainvoke(
        turn_input(mentioned_context()),
        context=TurnRuntime(speaker=speaker, journal=FakeJournal(), responder=FakeResponder()),
    )

    assert speaker.utterances == []


async def test_silent_decision_never_consults_the_responder_and_never_speaks() -> None:
    speaker = FakeSpeaker()
    responder = FakeResponder()
    graph = build_turn_graph(decider=FakeDecider(decision("silent")), window_turns=6)

    await graph.ainvoke(
        turn_input(mentioned_context()),
        context=TurnRuntime(speaker=speaker, journal=FakeJournal(), responder=responder),
    )

    assert responder.transcripts == []
    assert speaker.utterances == []


async def test_speak_decision_invokes_the_speaker_exactly_once() -> None:
    speaker = FakeSpeaker()
    graph = build_turn_graph(decider=FakeDecider(decision("speak")), window_turns=6)
    jev = await seeded_jev(FakeDecider(decision("speak")))

    await graph.ainvoke(
        turn_input(mentioned_context()),
        context=TurnRuntime(
            speaker=speaker, journal=FakeJournal(), responder=FakeResponder(), jev=jev
        ),
    )

    assert speaker.utterances == [TWIN_REPLY]


async def test_cached_decider_failure_falls_back_to_rules_without_failing_the_turn() -> None:
    speaker = FakeSpeaker()
    journal = FakeJournal()
    graph = build_turn_graph(decider=FakeDecider(None, error=TimeoutError()), window_turns=6)
    jev = await seeded_jev(FakeDecider(None, error=TimeoutError()))

    out = await graph.ainvoke(
        turn_input(mentioned_context()),
        context=TurnRuntime(speaker=speaker, journal=journal, responder=FakeResponder(), jev=jev),
    )

    assert out["failure"] is None
    assert speaker.utterances == []
    assert journal.names == ["policy_decider_failed", "policy_decided"]
    assert journal.fields[0] == {
        "source": "agent",
        "code": "decider_timeout",
        "decision_source": "rules",
    }


async def test_stop_event_skips_the_decider_and_never_invokes_the_speaker() -> None:
    decider = FakeDecider(None)
    speaker = FakeSpeaker()
    graph = build_turn_graph(decider=decider, window_turns=6)

    out = await graph.ainvoke(
        stop_input(),
        context=TurnRuntime(speaker=speaker, journal=FakeJournal(), responder=FakeResponder()),
    )

    assert decider.windows == []
    assert speaker.utterances == []
    assert out["speaking_generation"] == 5


async def test_speaking_generation_is_written_only_by_the_speak_node() -> None:
    graph = build_turn_graph(decider=FakeDecider(None), window_turns=6)
    writers: list[str] = []

    for model_input in (stop_input(), turn_input(addressed_context())):
        async for chunk in graph.astream(
            model_input,
            context=TurnRuntime(
                speaker=FakeSpeaker(), journal=FakeJournal(), responder=FakeResponder()
            ),
            stream_mode="updates",
        ):
            writers.extend(
                node for node, update in chunk.items() if "speaking_generation" in update
            )

    assert writers == ["speak", "speak"]
