import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessageChunk

from fakeyou.conversation import meeting_turns, turn_graph
from fakeyou.conversation.model_responder import ModelResponder, ReplierWholeResponder
from fakeyou.conversation.turn_graph import (
    SentenceLimits,
    WholeResponder,
    build_turn_graph,
    tool_start_hook,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import ReplyOptions, TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent

MODEL_NODE = {"langgraph_node": "model"}
GRAPH_SOURCE = Path(turn_graph.__file__).read_text(encoding="utf-8")
TURNS_SOURCE = Path(meeting_turns.__file__).read_text(encoding="utf-8")


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


class ToolPhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


class FakeReplier:
    def __init__(
        self,
        reply: str = "Yes, I am here.",
        *,
        on_call: Callable[[Callable[[], None]], Awaitable[None] | None] | None = None,
    ) -> None:
        self._reply = reply
        self._on_call = on_call
        self.events: list[tuple[TranscriptEvent, ...]] = []
        self.hooks: list[Callable[[], None]] = []

    @property
    def calls(self) -> list[tuple[str, ...]]:
        return [tuple(event.id for event in events) for events in self.events]

    async def respond(
        self, events: Sequence[TranscriptEvent], on_tool_start: Callable[[], None]
    ) -> str:
        self.events.append(tuple(events))
        self.hooks.append(on_tool_start)
        if self._on_call is not None:
            step = self._on_call(on_tool_start)
            if step is not None:
                await step
        return self._reply


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


class Harness:
    def __init__(self, replier: FakeReplier) -> None:
        self.journal = FakeJournal()
        self.session = FakeSession()
        self.moves: list[str] = []
        self.lifecycle = TurnLifecycle(
            build_turn_graph(decider=None, window_turns=3),
            journal=self.journal,
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        )
        self.lifecycle.observe(self._record_move)
        self.lifecycle.configure_reply(
            ReplyOptions(
                whole_turn=lambda events, hook: ReplierWholeResponder(replier, events, hook),
                sentences=SentenceLimits(max_sentences=2, language="en"),
            )
        )
        self.lifecycle.bind(self.session)
        self.replier = replier

    def _record_move(self, _previous: str, phase: str, _detail: str) -> None:
        self.moves.append(phase)

    async def say(self, event_id: str, text: str) -> None:
        await TurnIngress(self.lifecycle, addressing_names=("twin",)).handle_final(
            transcript(event_id, text)
        )


async def test_adapter_passes_the_events_tuple_and_tool_hook_and_returns_the_text() -> None:
    replier = FakeReplier(reply="Hi there.")
    hook_calls: list[str] = []
    events = (transcript("f1", "twin, hi"), transcript("f2", "twin, again"))
    adapter: WholeResponder = ReplierWholeResponder(
        replier, events, lambda: hook_calls.append("tool")
    )

    text = await adapter.respond("twin, again")

    assert text == "Hi there."
    assert replier.events == [events]
    replier.hooks[0]()
    assert hook_calls == ["tool"]


async def test_tool_start_during_the_reply_moves_acting_while_the_turn_is_current() -> None:
    harness = Harness(FakeReplier(on_call=lambda hook: hook()))

    await harness.say("f1", "twin, look it up")

    assert harness.moves == ["weighing", "acting", "queued", "listening"]
    assert harness.session.spoken == ["Yes, I am here."]


async def test_tool_start_after_a_newer_final_took_the_floor_does_not_move_acting() -> None:
    def newer_final_then_hook(hook: Callable[[], None]) -> None:
        harness.lifecycle.floor.advance()
        hook()

    harness = Harness(FakeReplier(on_call=newer_final_then_hook))

    await harness.say("f1", "twin, look it up")

    assert "acting" not in harness.moves
    assert harness.session.spoken == []


async def test_tool_start_after_close_does_not_move_acting() -> None:
    harness = Harness(FakeReplier())
    runtime = harness.lifecycle.runtime
    assert runtime is not None
    hook = tool_start_hook(
        runtime, harness.lifecycle.admitted.count, harness.lifecycle.floor.generation, "f1"
    )

    await harness.lifecycle.close()
    hook()

    assert "acting" not in harness.moves
    assert harness.session.spoken == []


async def test_two_pending_finals_are_answered_by_one_reply_that_covers_both() -> None:
    harness = Harness(FakeReplier())
    harness.lifecycle.queue.admit(transcript("f1", "twin, first"))

    await harness.say("f2", "twin, second")

    assert harness.replier.calls == [("f1", "f2")]
    assert harness.session.spoken == ["Yes, I am here."]
    assert harness.lifecycle.queue.pending is False


async def test_single_final_is_answered_alone() -> None:
    harness = Harness(FakeReplier())

    await harness.say("f1", "twin, only one")

    assert harness.replier.calls == [("f1",)]


def test_graph_drains_every_pending_final_into_one_respond_call() -> None:
    assert "batch = queue.take()" in GRAPH_SOURCE
    assert "work = _run_turn(runtime, speech, batch, scope, generation)" in GRAPH_SOURCE
    assert "return ReplierWholeResponder(replier, events, hook)" in TURNS_SOURCE
    assert "hook = tool_start_hook(runtime, generation, floor_generation, final.id)" in GRAPH_SOURCE
    assert "if runtime.admitted is not None and runtime.admitted.count != generation:" in (
        GRAPH_SOURCE
    )
