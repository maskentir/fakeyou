import asyncio
import inspect
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, override

from langchain_core.messages import AIMessageChunk

from fakeyou.conversation import conversation as conversation_module
from fakeyou.conversation import turn_graph, turn_ingress
from fakeyou.conversation.turn_graph import build_turn_graph
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent
from tests._model_responder import ModelResponder, ReplyTrace
from tests._prepared_support import PreparedSpeaker

MODEL_NODE = {"langgraph_node": "model"}
TOOLS_NODE = {"langgraph_node": "tools"}
GRAPH_SOURCE = "".join(
    Path(inspect.getfile(module)).read_text(encoding="utf-8")
    for module in (turn_graph, turn_ingress, conversation_module)
)


class FakeSession(PreparedSpeaker):
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.is_speaking = False

    @override
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

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class ToolPhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


def model_chunks(*pieces: str) -> Callable[[str], AsyncIterator[tuple[object, object]]]:
    async def chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
        for piece in pieces:
            yield AIMessageChunk(content=piece), MODEL_NODE

    return chunks


def transcript(event_id: str, text: str, *, participant_id: str = "p1") -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


class Harness:
    def __init__(
        self,
        chunks: Callable[[str], AsyncIterator[tuple[object, object]]] | None = None,
        *,
        max_sentences: int = 3,
        phase: ToolPhaseState | None = None,
        tool_acting: bool = False,
    ) -> None:
        self.journal = FakeJournal()
        self.session = FakeSession()
        self.moves: list[str] = []
        self.tool_phase = phase if phase is not None else ToolPhaseState()
        self.responder = ModelResponder(
            chunks=chunks if chunks is not None else model_chunks("Yes, I am here. "),
            phase=self.tool_phase,
            max_sentences=max_sentences,
            language="en",
            trace=ReplyTrace(
                journal=self.journal,
                request_id="f1:1",
                transcript_id="f1",
                on_tool_start=(lambda: self.lifecycle.move("acting", "f1"))
                if tool_acting
                else None,
            ),
        )
        self.lifecycle = TurnLifecycle(
            build_turn_graph(),
            journal=self.journal,
            responder=self.responder,
        )
        self.lifecycle.observe(self._record_move)
        self.lifecycle.bind(self.session)

    def _record_move(self, _previous: str, phase: str, _detail: str) -> None:
        self.moves.append(phase)

    def ingress(self, names: tuple[str, ...]) -> TurnIngress:
        return TurnIngress(self.lifecycle, addressing_names=names)


async def test_name_addressed_final_admits_and_replies() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    decision = await ingress.handle_final(transcript("f1", "twin, are you there?"))

    assert decision is not None
    assert decision.decision == "speak"
    assert harness.session.spoken == ["Yes, I am here."]


async def test_whole_phrase_name_match_rejects_a_longer_word() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    decision = await ingress.handle_final(transcript("f1", "twinkle, are you there?"))

    assert decision is not None
    assert decision.decision == "silent"
    assert decision.reason_code == "fallback_unnamed"
    assert harness.session.spoken == []


async def test_unaddressed_final_with_names_is_silent_and_journals_the_loop_reason() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    decision = await ingress.handle_final(transcript("f1", "okay"))

    assert decision is not None
    assert decision.decision == "silent"
    assert harness.session.spoken == []
    policy = harness.journal.of("policy_decided")
    assert len(policy) == 1
    assert policy[0]["transcript_id"] == "f1"
    assert policy[0]["action"] == "silent"
    assert policy[0]["reason"] == "fallback_unnamed"
    assert policy[0]["decision_source"] == "fallback"


async def test_unaddressed_final_without_names_is_admitted_like_the_loop() -> None:
    harness = Harness()
    ingress = harness.ingress(())

    decision = await ingress.handle_final(transcript("f1", "Alice, lunch at noon?"))

    assert decision is not None
    assert decision.decision == "speak"
    assert harness.session.spoken == ["Yes, I am here."]
    assert harness.journal.of("policy_decided") == []


async def test_bare_ack_not_after_a_bare_ack_is_admitted_without_names() -> None:
    harness = Harness()
    ingress = harness.ingress(())

    await ingress.handle_final(transcript("f1", "Alice, lunch at noon?"))
    decision = await ingress.handle_final(transcript("f2", "yeah"))

    assert decision is not None
    assert decision.decision == "speak"
    assert harness.journal.of("ack_decided") == []


async def test_queue_admits_a_second_final_after_the_first_finishes() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    first = await ingress.handle_final(transcript("f1", "twin, one?"))
    assert harness.lifecycle.queue.pending is False
    second = await ingress.handle_final(transcript("f2", "twin, two?"))

    assert first is not None
    assert first.decision == "speak"
    assert second is not None
    assert second.decision == "speak"
    assert harness.session.spoken == ["Yes, I am here.", "Yes, I am here."]
    assert harness.lifecycle.queue.pending is False


async def test_reply_path_journals_answer_events_with_the_loop_literals() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    await ingress.handle_final(transcript("f1", "twin, are you there?"))

    text = harness.journal.of("answer_text_emitted")
    sentences = harness.journal.of("answer_sentence_emitted")
    assert len(text) == 1
    assert text[0]["transcript_id"] == "f1"
    assert len(sentences) == 1
    assert sentences[0]["sentence_index"] == 0
    assert sentences[0]["chars"] == len("Yes, I am here.")
    assert sentences[0]["transcript_id"] == "f1"


async def test_reply_path_moves_queued_then_listening_after_weighing() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    await ingress.handle_final(transcript("f1", "twin, are you there?"))

    assert harness.moves == ["weighing", "queued", "listening"]


async def test_reply_with_no_sentence_moves_to_waiting() -> None:
    harness = Harness(model_chunks())
    ingress = harness.ingress(("twin",))

    await ingress.handle_final(transcript("f1", "twin, are you there?"))

    assert harness.moves == ["weighing", "waiting"]
    assert harness.session.spoken == []


async def test_tool_start_during_the_reply_moves_acting_before_listening() -> None:
    phase = ToolPhaseState()

    async def chunks(_transcript: str) -> AsyncIterator[tuple[object, object]]:
        yield AIMessageChunk(content="Let me check. "), MODEL_NODE
        phase.pending = True
        phase.boundary += 1
        yield AIMessageChunk(content=""), TOOLS_NODE
        phase.pending = False
        phase.boundary += 1
        yield AIMessageChunk(content="It is noon. "), MODEL_NODE

    harness = Harness(chunks, phase=phase, tool_acting=True)
    ingress = harness.ingress(("twin",))

    await ingress.handle_final(transcript("f1", "twin, what time is it?"))

    assert harness.moves == ["weighing", "queued", "acting", "listening"]


async def test_reply_over_the_sentence_cap_journals_reply_capped() -> None:
    harness = Harness(model_chunks("One. Two. "), max_sentences=1)
    ingress = harness.ingress(("twin",))

    await ingress.handle_final(transcript("f1", "twin, count?"))

    assert harness.session.spoken == ["One."]
    capped = harness.journal.of("reply_capped")
    assert len(capped) == 1
    assert capped[0]["chars_out"] == len("One.")


async def test_pending_queue_is_empty_again_after_the_next_reply() -> None:
    harness = Harness()
    ingress = harness.ingress(("twin",))

    harness.lifecycle.queue.admit(transcript("f0", "twin, queued?"))
    assert harness.lifecycle.queue.pending is True
    harness.lifecycle.queue.take()
    assert harness.lifecycle.queue.pending is False
    await ingress.handle_final(transcript("f1", "twin, now?"))

    assert harness.session.spoken == ["Yes, I am here."]


def test_reply_path_literals_are_the_graph_literals() -> None:
    for literal in (
        '"answer_text_emitted"',
        '"answer_sentence_emitted"',
        '"reply_capped"',
        '"queued"',
        '"listening"',
        '"waiting"',
        '"acting"',
    ):
        assert literal in GRAPH_SOURCE
