import asyncio
import inspect
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation import turn_graph, turn_recovery
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    SpeechFloor,
    SpeechInput,
    SpeechRuntime,
    TurnRuntime,
    build_speech_graph,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.transcript import TranscriptEvent


class SilentResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        sentences: tuple[str, ...] = ()
        for sentence in sentences:
            yield SpokenChunk(text=sentence, language=DEFAULTS.speech.language)


class HeldResponder:
    def __init__(self, sentences: Sequence[str], release: asyncio.Event) -> None:
        self._sentences = tuple(sentences)
        self._release = release
        self.transcripts: list[str] = []

    async def stream_reply(self, transcript: str) -> AsyncIterator[SpokenChunk]:
        self.transcripts.append(transcript)
        yield SpokenChunk(text=self._sentences[0], language=DEFAULTS.speech.language)
        await self._release.wait()
        for sentence in self._sentences[1:]:
            yield SpokenChunk(text=sentence, language=DEFAULTS.speech.language)


class FakeSink:
    def __init__(self) -> None:
        self.sentences: list[str] = []
        self.first_spoken = asyncio.Event()

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)
        self.first_spoken.set()


class SupersedingSink:
    def __init__(self, floor: SpeechFloor) -> None:
        self._floor = floor
        self.sentences: list[str] = []

    async def say(self, sentence: str, language: str) -> None:
        del language
        self.sentences.append(sentence)
        if len(self.sentences) == 1:
            self._floor.advance()


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


def stop_input(speaking_generation: int) -> Any:
    return {
        "event": TurnEvent(kind="stop", event_id="s1", at=2.0, payload=""),
        "context": ParticipationContext(addressed=False),
        "speaking_generation": speaking_generation,
    }


def speech_input(speaking_generation: int) -> SpeechInput:
    return SpeechInput(
        transcript="twin, what is the plan?",
        speaking_generation=speaking_generation,
    )


async def start_held_speech(
    floor: SpeechFloor, responder: HeldResponder, sink: Any, journal: FakeJournal
) -> asyncio.Task[Any]:
    graph = build_speech_graph()
    return asyncio.create_task(
        floor.hold(
            _run(
                graph,
                speech_input(floor.generation),
                SpeechRuntime(responder=responder, sink=sink, journal=journal, floor=floor),
            )
        )
    )


async def _run(graph: Any, model_input: SpeechInput, context: SpeechRuntime) -> None:
    await graph.ainvoke(model_input, context=context)


async def test_barge_in_cancels_the_active_speech_and_advances_the_generation() -> None:
    release = asyncio.Event()
    floor = SpeechFloor(generation=4)
    sink = FakeSink()
    responder = HeldResponder(["First sentence.", "Second sentence."], release)
    speech = await start_held_speech(floor, responder, sink, FakeJournal())
    await sink.first_spoken.wait()

    generation = await floor.interrupt()
    await speech

    assert generation == 5
    assert floor.generation == 5
    assert sink.sentences == ["First sentence."]
    assert speech.done()


async def test_stop_during_speech_ends_the_turn_without_a_new_reply() -> None:
    release = asyncio.Event()
    floor = SpeechFloor(generation=4)
    sink = FakeSink()
    responder = HeldResponder(["First sentence.", "Second sentence."], release)
    speech = await start_held_speech(floor, responder, sink, FakeJournal())
    await sink.first_spoken.wait()
    speaker = FakeSpeaker()
    turn_journal = FakeJournal()

    out = await build_turn_graph().ainvoke(
        stop_input(speaking_generation=4),
        context=TurnRuntime(
            speaker=speaker,
            journal=turn_journal,
            responder=SilentResponder(),
            floor=floor,
        ),
    )
    await speech

    assert out["speaking_generation"] == 5
    assert speaker.utterances == []
    assert turn_journal.names == []
    assert responder.transcripts == ["twin, what is the plan?"]
    assert sink.sentences == ["First sentence."]


async def test_late_chunk_from_a_superseded_generation_is_dropped() -> None:
    floor = SpeechFloor(generation=4)
    sink = SupersedingSink(floor)
    journal = FakeJournal()
    graph = build_speech_graph()
    release = asyncio.Event()
    release.set()
    responder = HeldResponder(["First sentence.", "Late sentence."], release)

    out = await graph.ainvoke(
        speech_input(4),
        context=SpeechRuntime(responder=responder, sink=sink, journal=journal, floor=floor),
    )

    assert sink.sentences == ["First sentence."]
    assert out["speaking_generation"] == 5
    assert journal.names == ["sentence_dropped"]
    assert journal.fields == [
        {
            "source": "agent",
            "request_id": None,
            "transcript_id": None,
            "sentence_index": 1,
            "detail": "stale_generation",
        }
    ]


BARGE_NAMES = ("barge_in_detected", "agent_cancel_requested", "agent_cancelled")
CANCEL_FIELDS = {"source", "request_id", "transcript_id", "generation", "reason", "duration_ms"}


class OneSentenceResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class BargeAudio:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.is_speaking = True

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


def barge_ingress(
    journal: FakeJournal, addressing_names: tuple[str, ...]
) -> tuple[TurnIngress, TurnLifecycle, BargeAudio]:
    lifecycle = TurnLifecycle(
        build_turn_graph(),
        journal=journal,
        responder=OneSentenceResponder(),
    )
    audio = BargeAudio()
    lifecycle.bind(audio)
    return TurnIngress(lifecycle, addressing_names=addressing_names), lifecycle, audio


def final_event(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="p1",
        speaker_evidence_id=None,
    )


def barge_names(journal: FakeJournal) -> list[str]:
    return [name for name in journal.names if name in BARGE_NAMES]


async def hold_until_cancelled() -> None:
    await asyncio.Event().wait()


async def settle(speech: asyncio.Task[bool]) -> bool:
    return await asyncio.wait_for(speech, timeout=2)


async def start_speaking(lifecycle: TurnLifecycle) -> asyncio.Task[bool]:
    speech = asyncio.create_task(lifecycle.floor.hold(hold_until_cancelled()))
    await asyncio.sleep(0)
    return speech


async def test_final_during_speech_cancels_the_turn_and_journals_barge_in() -> None:
    journal = FakeJournal()
    ingress, lifecycle, audio = barge_ingress(journal, addressing_names=("twin",))
    speech = await start_speaking(lifecycle)

    await ingress.handle_final(final_event("f1", "twin, are you there?"))

    assert await settle(speech) is False
    assert barge_names(journal) == [
        "agent_cancel_requested",
        "agent_cancelled",
        "barge_in_detected",
    ]
    barge = journal.fields[journal.names.index("barge_in_detected")]
    assert barge == {"source": "agent", "trigger": "final", "words": 4}
    assert audio.stops == 1
    assert audio.spoken == ["Yes, I am here."]


async def test_unaddressed_final_during_speech_yields_the_floor_without_cancelling() -> None:
    journal = FakeJournal()
    ingress, lifecycle, audio = barge_ingress(journal, addressing_names=("twin",))
    speech = await start_speaking(lifecycle)

    decision = await ingress.handle_final(final_event("f1", "Alice, lunch at noon today?"))

    assert decision is not None
    assert decision.decision == "silent"
    assert barge_names(journal) == ["barge_in_detected"]
    assert audio.stops == 1
    assert not speech.done()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_interim_during_speech_interrupts_and_journals_the_loop_names() -> None:
    journal = FakeJournal()
    ingress, lifecycle, audio = barge_ingress(journal, addressing_names=())
    speech = await start_speaking(lifecycle)

    await ingress.handle_interim("please stop talking now")

    assert await settle(speech) is False
    assert journal.names == ["barge_in_detected", "agent_cancel_requested", "agent_cancelled"]
    assert journal.fields[0] == {"source": "agent", "trigger": "interim", "words": 4}
    assert set(journal.fields[1]) == CANCEL_FIELDS
    assert journal.fields[1]["reason"] == "interim"
    assert journal.fields[2]["reason"] == "interim"
    assert audio.stops == 1


async def test_interim_below_the_minimum_words_does_not_interrupt() -> None:
    journal = FakeJournal()
    ingress, lifecycle, audio = barge_ingress(journal, addressing_names=())
    speech = await start_speaking(lifecycle)

    await ingress.handle_interim("stop now")

    assert not speech.done()
    assert journal.names == []
    assert audio.stops == 0
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)


async def test_interim_is_a_barge_in_while_only_twin_names_are_configured() -> None:
    journal = FakeJournal()
    ingress, lifecycle, audio = barge_ingress(journal, addressing_names=("twin",))
    speech = await start_speaking(lifecycle)

    await ingress.handle_interim("twin please stop talking")

    assert await settle(speech) is False
    assert barge_names(journal) == [
        "barge_in_detected",
        "agent_cancel_requested",
        "agent_cancelled",
    ]
    assert audio.stops == 1


async def test_cancelled_generation_drops_its_late_chunks() -> None:
    journal = FakeJournal()
    ingress, lifecycle, _ = barge_ingress(journal, addressing_names=())
    release = asyncio.Event()
    sink = FakeSink()
    responder = HeldResponder(["First sentence.", "Late sentence."], release)
    speech = asyncio.create_task(
        _run(
            build_speech_graph(),
            speech_input(lifecycle.floor.generation),
            SpeechRuntime(
                responder=responder,
                sink=sink,
                journal=FakeJournal(),
                floor=lifecycle.floor,
            ),
        )
    )
    await sink.first_spoken.wait()

    await ingress.handle_interim("please stop talking now")
    release.set()
    await asyncio.wait_for(speech, timeout=2)

    assert sink.sentences == ["First sentence."]
    assert lifecycle.floor.busy is False


def test_journal_names_are_the_turn_graph_string_literals() -> None:
    graph_source = "".join(
        Path(inspect.getfile(module)).read_text(encoding="utf-8")
        for module in (turn_graph, turn_recovery)
    )

    for name in (*BARGE_NAMES, "sentence_dropped"):
        assert f'"{name}"' in graph_source
