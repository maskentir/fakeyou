import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessageChunk

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.model_responder import ModelResponder
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import (
    SpeechInput,
    SpeechRuntime,
    build_speech_graph,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent

MODEL_NODE = {"langgraph_node": "model"}
TOOLS_NODE = {"langgraph_node": "tools"}


class FakeSession:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.is_speaking = False

    async def speak(self, text: str, **_: Any) -> None:
        self.spoken.append(text)

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

    def record(self, event: str, **_: Any) -> None:
        self.names.append(event)


class FakeSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)


class FakeResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class PhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


def transcript(
    event_id: str,
    text: str,
    *,
    finalized: bool = True,
    participant_id: str = "p1",
) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=finalized,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def bound_ingress(journal: FakeJournal) -> tuple[TurnIngress, TurnLifecycle, FakeSession]:
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=None, window_turns=3),
        journal=journal,
        responder=FakeResponder(),
    )
    session = FakeSession()
    lifecycle.bind(session)
    return TurnIngress(lifecycle, addressing_names=("twin",)), lifecycle, session


async def test_addressed_final_reaches_participation_and_advances_speaking_generation() -> None:
    journal = FakeJournal()
    ingress, lifecycle, session = bound_ingress(journal)

    decision = await ingress.handle_final(transcript("f1", "twin, are you there?"))

    assert decision is not None
    assert decision.decision == "speak"
    assert lifecycle.floor.generation == 1
    assert len(session.spoken) == 1


async def test_unaddressed_final_stays_silent_and_keeps_speaking_generation() -> None:
    journal = FakeJournal()
    ingress, lifecycle, session = bound_ingress(journal)

    decision = await ingress.handle_final(transcript("f1", "Alice, lunch at noon?"))

    assert decision is not None
    assert decision.decision == "silent"
    assert lifecycle.floor.generation == 0
    assert session.spoken == []


async def test_duplicate_final_is_admitted_once() -> None:
    journal = FakeJournal()
    ingress, lifecycle, _ = bound_ingress(journal)
    event = transcript("f1", "twin, are you there?")

    await ingress.handle_final(event)
    second = await ingress.handle_final(event)

    assert second is None
    assert lifecycle.floor.generation == 1


async def test_paused_final_is_journaled_as_suppressed_without_participation() -> None:
    journal = FakeJournal()
    ingress, lifecycle, session = bound_ingress(journal)
    await lifecycle.pause()

    decision = await ingress.handle_final(transcript("f1", "twin, are you there?"))

    assert decision is None
    assert journal.names == ["transcript_suppressed"]
    assert lifecycle.floor.generation == 0
    assert session.spoken == []


async def test_interim_never_replies_and_journals_nothing() -> None:
    journal = FakeJournal()
    ingress, lifecycle, session = bound_ingress(journal)

    await ingress.handle_interim("twin, are you there")

    assert lifecycle.floor.generation == 0
    assert session.spoken == []
    assert journal.names == []


async def test_responder_adapter_forwards_only_model_text_through_the_sentence_cap() -> None:
    async def chunks(_: str) -> AsyncIterator[tuple[object, object]]:
        yield AIMessageChunk(content="Sure thing. "), MODEL_NODE
        yield AIMessageChunk(content="ignored tool output. "), TOOLS_NODE
        yield AIMessageChunk(content="Second one here. Third one. Fourth one."), MODEL_NODE

    adapter = ModelResponder(chunks=chunks, phase=PhaseState(), max_sentences=2, language="en")
    sink = FakeSink()

    await build_speech_graph().ainvoke(
        SpeechInput(transcript="twin, what now?", speaking_generation=0),
        context=SpeechRuntime(responder=adapter, sink=sink, journal=FakeJournal()),
    )

    assert sink.sentences == ["Sure thing.", "Second one here."]


async def test_responder_adapter_resets_partial_text_at_a_tool_boundary() -> None:
    phase = PhaseState()

    async def chunks(_: str) -> AsyncIterator[tuple[object, object]]:
        yield AIMessageChunk(content="Half a sentence"), MODEL_NODE
        phase.pending = True
        phase.boundary = 1
        yield AIMessageChunk(content="leaked text."), MODEL_NODE
        phase.pending = False
        yield AIMessageChunk(content="Fresh start."), MODEL_NODE

    adapter = ModelResponder(chunks=chunks, phase=phase, max_sentences=3, language="en")
    sentences = [sentence async for sentence in adapter.stream_reply("twin, go on")]

    assert [chunk.text for chunk in sentences] == ["Fresh start."]
