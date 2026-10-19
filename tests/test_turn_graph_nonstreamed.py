import asyncio
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessageChunk

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation import turn_graph, turn_recovery
from fakeyou.conversation.model_responder import ModelResponder
from fakeyou.conversation.turn_graph import (
    SentenceLimits,
    SpeechFloor,
    SpeechRuntime,
    TurnRuntime,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import ReplyOptions, TurnLifecycle
from fakeyou.conversation.turn_recovery import RecoveryOptions
from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.transcript import TranscriptEvent
from tests._audio_fakes import SilentSpeaker

MODEL_NODE = {"langgraph_node": "model"}
GRAPH_SOURCE = Path(turn_graph.__file__).read_text(encoding="utf-8")
RECOVERY_SOURCE = Path(turn_recovery.__file__).read_text(encoding="utf-8")
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


class ToolPhaseState:
    def __init__(self) -> None:
        self.pending = False
        self.boundary = 0


class FakeWholeResponder:
    def __init__(self, reply: str = "Yes, I am here.", delay: float = 0.0) -> None:
        self._reply = reply
        self._delay = delay
        self.calls: list[str] = []
        self.on_call: Callable[[], Any] | None = None

    async def respond(self, transcript: str) -> str:
        self.calls.append(transcript)
        if self.on_call is not None:
            await self.on_call()
        if self._delay:
            await asyncio.sleep(self._delay)
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
    def __init__(
        self,
        whole: FakeWholeResponder,
        *,
        timeout: float = 60.0,
        max_sentences: int = 2,
    ) -> None:
        self.journal = FakeJournal()
        self.session = FakeSession()
        self.lifecycle = TurnLifecycle(
            build_turn_graph(decider=None, window_turns=3),
            journal=self.journal,
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        )
        self.lifecycle.configure_recovery(
            RecoveryOptions(language="en", response_timeout_seconds=timeout)
        )
        self.lifecycle.configure_reply(
            ReplyOptions(
                whole=whole, sentences=SentenceLimits(max_sentences=max_sentences, language="en")
            )
        )
        self.lifecycle.bind(self.session)

    async def say(self, event_id: str, text: str) -> None:
        await TurnIngress(self.lifecycle, addressing_names=("twin",)).handle_final(
            transcript(event_id, text)
        )


class AdvancingSink:
    def __init__(self, floor: SpeechFloor) -> None:
        self._floor = floor

    async def say(self, _sentence: str) -> None:
        self._floor.advance()


class StallingSink:
    async def say(self, _sentence: str) -> None:
        await asyncio.sleep(1.0)


class StallingStream:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[str]:
        await asyncio.sleep(1.0)
        yield "Late sentence."


class SilentSink:
    async def say(self, _sentence: str) -> None:
        return None


async def test_whole_reply_is_spoken_once_and_journaled_as_agent_finished() -> None:
    harness = Harness(FakeWholeResponder("Yes, I am here."))

    await harness.say("f1", "twin, are you there?")

    assert harness.session.spoken == ["Yes, I am here."]
    assert [fields["request_id"] for fields in harness.journal.of("agent_started")] == ["f1:1"]
    assert [fields["request_id"] for fields in harness.journal.of("agent_finished")] == ["f1:1"]
    assert harness.journal.of("agent_failed") == []
    assert harness.journal.names.index("agent_started") < harness.journal.names.index(
        "agent_finished"
    )
    assert harness.lifecycle.phase == "listening"
    assert harness.lifecycle.recovery.consecutive_failures == 0


async def test_whole_reply_over_the_sentence_cap_is_capped_and_journaled() -> None:
    harness = Harness(FakeWholeResponder("First one. Second one. Third one."), max_sentences=2)

    await harness.say("f1", "twin, count for me")

    assert harness.session.spoken == ["First one. Second one."]
    assert harness.journal.of("reply_capped") == [
        {
            "source": "agent",
            "request_id": "f1:1",
            "chars_in": len("First one. Second one. Third one."),
            "chars_out": len("First one. Second one."),
        }
    ]


async def test_whole_reply_under_the_cap_is_not_journaled_as_capped() -> None:
    harness = Harness(FakeWholeResponder("Short one."))

    await harness.say("f1", "twin, short please")

    assert harness.session.spoken == ["Short one."]
    assert harness.journal.of("reply_capped") == []


async def test_whole_respond_over_the_turn_budget_is_cut_journaled_and_counted() -> None:
    harness = Harness(FakeWholeResponder(delay=1.0), timeout=0.05)

    await harness.say("f1", "twin, are you there?")

    assert harness.session.spoken == [TURN_FAILED_EN]
    assert harness.journal.of("turn_failed") == [
        {"source": "agent", "transcript_id": "f1", "error": "TimeoutError", "consecutive": 1}
    ]
    assert [fields["request_id"] for fields in harness.journal.of("agent_failed")] == ["f1:1"]
    assert harness.journal.of("agent_finished") == []
    assert harness.lifecycle.recovery.consecutive_failures == 1
    assert harness.lifecycle.phase == "waiting"


async def test_third_consecutive_whole_timeout_fails_without_a_third_line() -> None:
    harness = Harness(FakeWholeResponder(delay=1.0), timeout=0.05)

    for event_id in ("f1", "f2", "f3"):
        await harness.say(event_id, "twin, are you there?")

    assert harness.lifecycle.is_failed
    assert harness.session.spoken == [TURN_FAILED_EN, TURN_FAILED_EN]
    assert [fields["consecutive"] for fields in harness.journal.of("turn_failed")] == [1, 2, 3]


async def test_whole_reply_while_paused_is_suppressed_with_the_loop_reason() -> None:
    responder = FakeWholeResponder("Yes, I am here.")
    harness = Harness(responder)
    responder.on_call = harness.lifecycle.pause

    await harness.say("f1", "twin, are you there?")

    assert harness.session.spoken == []
    assert harness.journal.of("turn_suppressed") == [
        {"source": "agent", "request_id": "f1:1", "reason": "paused"}
    ]
    assert harness.journal.of("agent_finished") != []
    assert harness.lifecycle.recovery.consecutive_failures == 0


def test_default_response_budget_is_the_loop_budget() -> None:
    assert DEFAULTS.conversation.response_timeout_seconds == 60.0
    speech = SpeechRuntime(responder=StallingStream(), sink=SilentSink(), journal=FakeJournal())
    turn = TurnRuntime(speaker=SilentSpeaker(), journal=FakeJournal(), responder=StallingStream())
    assert speech.response_timeout_seconds == DEFAULTS.conversation.response_timeout_seconds
    assert turn.response_timeout_seconds == DEFAULTS.conversation.response_timeout_seconds


def test_turn_budget_literals_are_the_ones_the_graph_uses() -> None:
    assert GRAPH_SOURCE.count("async with asyncio.timeout(seconds):") == 1
    assert GRAPH_SOURCE.count("async with asyncio.timeout(budget):") == 0
    assert RECOVERY_SOURCE.count("async with asyncio.timeout(seconds):") == 1
    assert '"turn_suppressed", source="agent", request_id=request_id, reason="paused"' in (
        GRAPH_SOURCE
    )
    assert '"agent_finished",\n        source="agent",\n        request_id=scope.request_id,' in (
        RECOVERY_SOURCE
    )
    assert '"agent_failed",\n        source="agent",\n        request_id=scope.request_id,' in (
        RECOVERY_SOURCE
    )
