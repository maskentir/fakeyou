import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation import turn_graph, turn_lifecycle
from fakeyou.conversation.model_responder import ReplierWholeResponder
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import (
    Responder,
    SentenceLimits,
    ToolStartHook,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import ReplyOptions, TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent
from tests._model_responder import ModelResponder

MODEL_NODE = {"langgraph_node": "model"}
GRAPH_SOURCE = Path(turn_graph.__file__).read_text(encoding="utf-8")
LIFECYCLE_SOURCE = Path(turn_lifecycle.__file__).read_text(encoding="utf-8")
TRANSCRIPT = "twin, what is the plan?"


class FakeSession:
    def __init__(self, *, block_first: bool = False) -> None:
        self.spoken: list[str] = []
        self.is_speaking = False
        self.speaking = asyncio.Event()
        self._block_first = block_first

    async def speak(self, text: str, **_: Any) -> None:
        self.spoken.append(text)
        self.speaking.set()
        if self._block_first and len(self.spoken) == 1:
            await asyncio.Event().wait()

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

    def of(self, event: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.entries if name == event]


class ToolPhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


def unused_chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
    async def chunks() -> AsyncIterator[tuple[object, object]]:
        yield AIMessageChunk(content="unused. "), MODEL_NODE

    return chunks()


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


class FakeReplier:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    async def respond(
        self, events: Sequence[TranscriptEvent], _on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        self.batches.append(tuple(event.id for event in events))
        return SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class FixedStream:
    def __init__(self, batch: tuple[str, ...], batches: list[tuple[str, ...]]) -> None:
        self._batch = batch
        self._batches = batches

    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        self._batches.append(self._batch)
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class Harness:
    def __init__(self, mode: str, *, block_first: bool = False, bound: bool = True) -> None:
        self.mode = mode
        self.journal = FakeJournal()
        self.session = FakeSession(block_first=block_first)
        self.replier = FakeReplier()
        self.stream_batches: list[tuple[str, ...]] = []
        self.lifecycle = TurnLifecycle(
            build_turn_graph(decider=None, window_turns=3),
            journal=self.journal,
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        )
        sentences = SentenceLimits(max_sentences=2, language="en")
        if mode == "whole":
            options = ReplyOptions(
                whole_turn=lambda events, hook: ReplierWholeResponder(self.replier, events, hook),
                sentences=sentences,
            )
        else:
            options = ReplyOptions(stream_turn=self._stream_turn, sentences=sentences)
        self.lifecycle.configure_reply(options)
        if bound:
            self.lifecycle.bind(self.session)
        self.ingress = TurnIngress(self.lifecycle, addressing_names=("twin",))

    def _stream_turn(
        self, events: tuple[TranscriptEvent, ...], _hook: ToolStartHook, _request_id: str
    ) -> Responder:
        return FixedStream(tuple(event.id for event in events), self.stream_batches)

    def batches(self) -> list[tuple[str, ...]]:
        return self.replier.batches if self.mode == "whole" else self.stream_batches

    async def say(self, event_id: str, text: str = TRANSCRIPT) -> None:
        await self.ingress.handle_final(transcript(event_id, text))


@pytest.mark.parametrize("mode", ["whole", "streamed"])
async def test_sequential_finals_get_the_loop_request_ids(mode: str) -> None:
    harness = Harness(mode)

    await harness.say("f1", "twin, first")
    await harness.say("f2", "twin, second")
    await harness.say("f3", "twin, third")

    started = [fields["request_id"] for fields in harness.journal.of("agent_started")]
    finished = [fields["request_id"] for fields in harness.journal.of("agent_finished")]
    assert started == ["f1:1", "f2:2", "f3:3"]
    assert finished == ["f1:1", "f2:2", "f3:3"]


async def test_barge_in_cancel_carries_the_request_id_of_the_cancelled_turn() -> None:
    harness = Harness("whole", block_first=True)

    first = asyncio.create_task(harness.say("f1", "twin, first"))
    await harness.session.speaking.wait()
    await harness.say("f2", "twin, second")
    await first

    cancel_requests = harness.journal.of("agent_cancel_requested")
    assert [fields["request_id"] for fields in cancel_requests] == ["f1:1"]
    started = [fields["request_id"] for fields in harness.journal.of("agent_started")]
    assert started == ["f1:1", "f2:2"]


@pytest.mark.parametrize("mode", ["whole", "streamed"])
async def test_finals_admitted_before_the_drain_runs_join_one_batch(mode: str) -> None:
    harness = Harness(mode, bound=False)

    await harness.ingress.receive_final(transcript("A", "twin, first"))
    await harness.ingress.receive_final(transcript("B", "twin, second"))
    harness.lifecycle.bind(harness.session)
    await harness.lifecycle.wait_idle()

    assert harness.batches() == [("A", "B")]
    started = [fields["request_id"] for fields in harness.journal.of("agent_started")]
    assert started == ["B:2"]


def test_graph_numbers_requests_by_admitted_final_and_writes_the_same_messages() -> None:
    assert GRAPH_SOURCE.count("_advance_admitted(runtime)") == 1
    assert "queue.admit(final)\n    _advance_admitted(runtime)" in GRAPH_SOURCE
    assert "self._closed = True\n        self._admitted.advance()" in LIFECYCLE_SOURCE
    assert "request_id=turn_request_id(transcript_id, generation)," in GRAPH_SOURCE
    assert "request_id = turn_request_id(final.id, generation)" in GRAPH_SOURCE
