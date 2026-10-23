import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, override

from langchain_core.messages import AIMessageChunk

from fakeyou.conversation import turn_graph, turn_recovery
from fakeyou.conversation.model_responder import ModelResponder
from fakeyou.conversation.turn_graph import (
    SpeechFloor,
    SpeechRuntime,
    build_speech_graph,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.conversation.turn_recovery import (
    RecoveryOptions,
    TurnScope,
    raised_where,
    turn_request_id,
)
from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.taxonomy import Subsystem
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal

MODEL_NODE = {"langgraph_node": "model"}
GRAPH_SOURCE = "".join(
    Path(inspect.getfile(module)).read_text(encoding="utf-8")
    for module in (turn_graph, turn_recovery)
)
TURN_FAILED_EN: str = json.loads(read_package_text("spoken_lines.json"))["turn_failed"]["en"]


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
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.entries.append((event, fields))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class FaultJournal(Journal):
    def __init__(self) -> None:
        self.names: list[str] = []

    @override
    def record(self, event: str, *, source: Subsystem, **_: Any) -> str | None:
        del source
        self.names.append(event)
        return ""


class ToolPhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


def chunks_of(*pieces: str) -> Callable[[str], AsyncIterator[tuple[object, object]]]:
    async def chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
        for piece in pieces:
            yield AIMessageChunk(content=piece), MODEL_NODE

    return chunks


def stalling_chunks(
    first: str, rest: str, pause: float
) -> Callable[[str], AsyncIterator[tuple[object, object]]]:
    async def chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
        yield AIMessageChunk(content=first), MODEL_NODE
        await asyncio.sleep(pause)
        yield AIMessageChunk(content=rest), MODEL_NODE

    return chunks


def failing_chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
    async def chunks() -> AsyncIterator[tuple[object, object]]:
        raise RuntimeError("model down")
        yield AIMessageChunk(content=""), MODEL_NODE

    return chunks()


def flaky_chunks(failures: int) -> Callable[[str], AsyncIterator[tuple[object, object]]]:
    remaining = [failures]

    def chunks(transcript: str) -> AsyncIterator[tuple[object, object]]:
        if remaining[0] > 0:
            remaining[0] -= 1
            return failing_chunks(transcript)
        return chunks_of("Yes, I am here. ")(transcript)

    return chunks


def transcript(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="p1",
        speaker_evidence_id=None,
    )


class Harness:
    def __init__(
        self,
        chunks: Callable[[str], AsyncIterator[tuple[object, object]]],
        *,
        timeout: float = 60.0,
        faults: FaultReporter | None = None,
    ) -> None:
        self.journal = FakeJournal()
        self.session = FakeSession()
        responder = ModelResponder(
            chunks=chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
        )
        self.lifecycle = TurnLifecycle(
            build_turn_graph(decider=None, window_turns=3),
            journal=self.journal,
            responder=responder,
        )
        self.lifecycle.configure_recovery(
            RecoveryOptions(faults=faults, language="en", response_timeout_seconds=timeout)
        )
        self.lifecycle.bind(self.session)

    async def say(self, event_id: str, text: str) -> None:
        await TurnIngress(self.lifecycle, addressing_names=("twin",)).handle_final(
            transcript(event_id, text)
        )


class StreamedReplies:
    def __init__(self, sentences: list[str]) -> None:
        self._sentences = sentences

    async def stream_reply(self, _transcript: str) -> AsyncIterator[str]:
        for sentence in self._sentences:
            yield sentence


class AdvancingSink:
    def __init__(self, floor: SpeechFloor) -> None:
        self._floor = floor
        self.sentences: list[str] = []

    async def say(self, sentence: str) -> None:
        self.sentences.append(sentence)
        self._floor.advance()


async def test_sentence_over_the_budget_times_out_and_the_turn_recovers() -> None:
    harness = Harness(stalling_chunks("First sentence. ", "Second sentence. ", 1.0), timeout=0.05)

    await harness.say("f1", "twin, are you there?")

    assert harness.session.spoken == ["First sentence.", TURN_FAILED_EN]
    [failed] = harness.journal.of("turn_failed")
    assert {key: failed[key] for key in ("source", "transcript_id", "error", "consecutive")} == {
        "source": "agent",
        "transcript_id": "f1",
        "error": "TimeoutError",
        "consecutive": 1,
    }
    assert failed["raised_at"]
    assert [fields["request_id"] for fields in harness.journal.of("agent_failed")] == ["f1:1"]
    assert harness.journal.of("agent_finished") == []
    assert harness.lifecycle.phase == "waiting"


async def test_responder_error_counts_one_failure_and_records_a_fault() -> None:
    faults = FaultReporter(journal=FaultJournal())
    harness = Harness(failing_chunks, faults=faults)

    await harness.say("f1", "twin, are you there?")

    [failed] = harness.journal.of("turn_failed")
    assert {key: failed[key] for key in ("source", "transcript_id", "error", "consecutive")} == {
        "source": "agent",
        "transcript_id": "f1",
        "error": "RuntimeError",
        "consecutive": 1,
    }
    assert failed["raised_at"]
    assert harness.lifecycle.recovery.consecutive_failures == 1
    assert [fault.source for fault in faults.summaries()] == ["agent"]
    assert harness.session.spoken == [TURN_FAILED_EN]
    assert harness.lifecycle.phase == "waiting"


async def test_third_consecutive_failure_fails_the_conversation_without_the_line() -> None:
    harness = Harness(failing_chunks)

    for event_id in ("f1", "f2", "f3"):
        await harness.say(event_id, "twin, are you there?")

    assert [fields["consecutive"] for fields in harness.journal.of("turn_failed")] == [1, 2, 3]
    assert harness.session.spoken == [TURN_FAILED_EN, TURN_FAILED_EN]
    assert harness.lifecycle.is_failed is True


async def test_a_successful_turn_resets_the_consecutive_count() -> None:
    harness = Harness(flaky_chunks(failures=1))

    await harness.say("f1", "twin, are you there?")
    assert harness.lifecycle.recovery.consecutive_failures == 1
    await harness.say("f2", "twin, still there?")
    assert harness.lifecycle.recovery.consecutive_failures == 0
    assert harness.session.spoken == [TURN_FAILED_EN, "Yes, I am here."]


async def test_agent_events_carry_the_loop_literals_and_request_id() -> None:
    harness = Harness(chunks_of("Yes, I am here. "))

    await harness.say("f1", "twin, are you there?")

    started = harness.journal.of("agent_started")
    finished = harness.journal.of("agent_finished")
    assert [set(fields) for fields in started] == [{"source", "request_id"}]
    assert started[0]["request_id"] == "f1:1"
    assert [set(fields) for fields in finished] == [{"source", "request_id", "duration_ms"}]
    assert finished[0]["request_id"] == "f1:1"


def test_agent_event_literals_and_request_id_are_the_graph_literals() -> None:
    for literal in (
        '"agent_started"',
        '"agent_finished"',
        '"agent_failed"',
        '"turn_failed"',
        '"sentence_dropped"',
        '"agent_cancelled"',
        'f"{transcript_id}:{generation}"',
        "MAX_CONSECUTIVE_TURN_FAILURES = 3",
        "sentence = await anext(iterator)",
        "async with asyncio.timeout(seconds):",
    ):
        assert literal in GRAPH_SOURCE
    assert turn_request_id("f1", 1) == "f1:1"


async def test_dropped_sentence_journals_sentence_dropped_then_agent_cancelled() -> None:
    floor = SpeechFloor(generation=4)
    sink = AdvancingSink(floor)
    journal = FakeJournal()
    scope = TurnScope(
        request_id=turn_request_id("f1", 1),
        transcript_id="f1",
        generation=1,
        started=0.0,
        clock=lambda: 1.0,
        response_timeout_seconds=60.0,
    )

    out = await build_speech_graph().ainvoke(
        {
            "transcript": "twin, hi",
            "speaking_generation": 4,
            "transcript_event": None,
        },
        context=SpeechRuntime(
            responder=StreamedReplies(["First sentence.", "Second sentence."]),
            sink=sink,
            journal=journal,
            floor=floor,
            transcript_id="f1",
            scope=scope,
        ),
    )

    assert sink.sentences == ["First sentence."]
    assert out["failure"] is None
    assert journal.of("sentence_dropped") == [
        {
            "source": "agent",
            "request_id": "f1:1",
            "transcript_id": "f1",
            "sentence_index": 1,
            "detail": "stale_generation",
        }
    ]
    cancelled = journal.of("agent_cancelled")
    assert [fields["reason"] for fields in cancelled] == ["new_final"]
    assert cancelled[0]["generation"] == 1
    assert cancelled[0]["request_id"] == "f1:1"
    assert journal.names.index("sentence_dropped") < journal.names.index("agent_cancelled")


def test_a_failure_names_where_it_was_raised_and_the_last_package_frame() -> None:
    def explode() -> None:
        raise TypeError("boom")

    try:
        explode()
    except TypeError as error:
        where = raised_where(error)

    assert where["raised_at"].endswith(":explode")
    assert "raised_in" not in where
    assert raised_where(TypeError("never raised")) == {}
