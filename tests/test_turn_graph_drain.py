import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, override

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.model_responder import ReplierWholeResponder
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_graph import (
    SentenceLimits,
    ToolStartHook,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import ReplyOptions, TurnLifecycle
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter


class Session:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.indices: list[int | None] = []
        self.transcripts: list[str | None] = []
        self.is_speaking = False

    async def speak(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        del language
        self.spoken.append(text)
        self.indices.append(sentence_index)
        self.transcripts.append(transcript_id)

    async def stop_playback(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()


class Journal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.entries.append((event, fields))

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class RecordingFaults(FaultReporter):
    def __init__(self) -> None:
        self.recorded: list[Any] = []

    @override
    def record(self, fault: Any) -> None:
        self.recorded.append(fault)


class GatedReplier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.gate = asyncio.Event()

    async def respond(
        self, events: Sequence[TranscriptEvent], _on_tool_start: Callable[[], None]
    ) -> SpokenChunk:
        self.calls.append(tuple(event.id for event in events))
        self.started.set()
        try:
            if len(self.calls) == 1:
                await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return SpokenChunk(text=f"Reply to {events[-1].id}.", language=DEFAULTS.speech.language)


class SentenceSource:
    def __init__(self, sentences: Sequence[str], gate: asyncio.Event | None = None) -> None:
        self._sentences = tuple(sentences)
        self._gate = gate
        self.requests: list[str] = []
        self.hooks: list[ToolStartHook] = []

    def responder(
        self, _events: tuple[TranscriptEvent, ...], hook: ToolStartHook, request_id: str
    ) -> "SentenceStream":
        self.requests.append(request_id)
        self.hooks.append(hook)
        return SentenceStream(self._sentences, self._gate)


class SentenceStream:
    def __init__(self, sentences: tuple[str, ...], gate: asyncio.Event | None) -> None:
        self._sentences = sentences
        self._gate = gate

    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        for index, sentence in enumerate(self._sentences):
            if index == 1 and self._gate is not None:
                await self._gate.wait()
            yield SpokenChunk(text=sentence, language=DEFAULTS.speech.language)


SILENT = SentenceStream((), None)


def final(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="p1",
        speaker_evidence_id=None,
    )


def whole_lifecycle(replier: GatedReplier, journal: Journal) -> TurnLifecycle:
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=None, window_turns=3), journal=journal, responder=SILENT
    )
    lifecycle.configure_reply(
        ReplyOptions(
            whole_turn=lambda events, hook: ReplierWholeResponder(replier, events, hook),
            sentences=SentenceLimits(max_sentences=2, language="en"),
        )
    )
    return lifecycle


async def test_admission_returns_before_the_reply_runs_in_the_background() -> None:
    replier, journal, session = GatedReplier(), Journal(), Session()
    lifecycle = whole_lifecycle(replier, journal)
    lifecycle.bind(session)
    ingress = TurnIngress(lifecycle, addressing_names=())
    replier.gate.set()

    await ingress.receive_final(final("f1", "hello"))

    assert replier.calls == []
    await lifecycle.wait_idle()
    assert session.spoken == ["Reply to f1."]


async def test_new_final_cancels_the_reply_still_waiting_for_the_model() -> None:
    replier, journal, session = GatedReplier(), Journal(), Session()
    lifecycle = whole_lifecycle(replier, journal)
    lifecycle.bind(session)
    ingress = TurnIngress(lifecycle, addressing_names=())

    first = asyncio.create_task(ingress.handle_final(final("f1", "first")))
    await asyncio.wait_for(replier.started.wait(), 2)
    await ingress.handle_final(final("f2", "second"))
    await first

    assert replier.cancelled.is_set()
    assert replier.calls == [("f1",), ("f2",)]
    assert session.spoken == ["Reply to f2."]
    [requested] = journal.of("agent_cancel_requested")
    assert (requested["request_id"], requested["generation"], requested["reason"]) == (
        "f1:1",
        1,
        "new_final",
    )


async def test_interim_cancels_the_reply_still_waiting_for_the_model() -> None:
    replier, journal, session = GatedReplier(), Journal(), Session()
    lifecycle = whole_lifecycle(replier, journal)
    lifecycle.bind(session)
    ingress = TurnIngress(lifecycle, addressing_names=())

    await ingress.receive_final(final("f1", "first"))
    await asyncio.wait_for(replier.started.wait(), 2)
    await ingress.handle_interim("please hold on a moment")

    assert replier.cancelled.is_set()
    [requested] = journal.of("agent_cancel_requested")
    assert (requested["request_id"], requested["reason"]) == ("f1:1", "interim")
    await lifecycle.wait_idle()
    assert session.spoken == []


async def test_close_advances_the_admitted_count_like_a_new_generation() -> None:
    lifecycle = whole_lifecycle(GatedReplier(), Journal())
    before = lifecycle.admitted.count

    await lifecycle.close()

    assert lifecycle.admitted.count == before + 1


async def test_shadow_final_is_decided_but_never_answered() -> None:
    replier, journal, session = GatedReplier(), Journal(), Session()
    lifecycle = whole_lifecycle(replier, journal)
    lifecycle.configure_shadow(enabled=True)
    lifecycle.bind(session)
    ingress = TurnIngress(lifecycle, addressing_names=("twin",))

    await ingress.handle_final(final("f1", "twin, status please"))

    assert replier.calls == []
    assert [fields["reason"] for fields in journal.of("policy_decided")] == ["addressed"]
    assert [fields["reason"] for fields in journal.of("transcript_suppressed")] == ["shadow"]


async def test_streamed_sentences_reach_the_session_with_index_and_transcript() -> None:
    source, journal, session = SentenceSource(["One.", "Two."]), Journal(), Session()
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=None, window_turns=3), journal=journal, responder=SILENT
    )
    lifecycle.configure_reply(ReplyOptions(stream_turn=source.responder))
    lifecycle.bind(session)

    await TurnIngress(lifecycle, addressing_names=()).handle_final(final("f1", "hello"))

    assert session.spoken == ["One.", "Two."]
    assert session.indices == [0, 1]
    assert session.transcripts == ["f1", "f1"]
    assert source.requests == ["f1:1"]
