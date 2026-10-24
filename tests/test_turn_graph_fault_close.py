import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, override

import pytest

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation import turn_graph, turn_lifecycle
from fakeyou.conversation.jev_refresh import JevState
from fakeyou.conversation.participation_decider import DeciderTurn, ParticipationDecision
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.session import ConversationError
from fakeyou.conversation.turn_graph import (
    SentenceLimits,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import ReplyOptions, TurnLifecycle
from fakeyou.conversation.turn_recovery import RecoveryOptions
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.provider_faults import ProviderFault
from tests._audio_fakes import wait_until

GRAPH_SOURCE = Path(turn_graph.__file__).read_text(encoding="utf-8")
LIFECYCLE_SOURCE = Path(turn_lifecycle.__file__).read_text(encoding="utf-8")


class Session:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stops = 0
        self.is_speaking = False

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


class Journal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def record(self, event: str, **fields: Any) -> None:
        self.entries.append((event, fields))

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class RecordingFaults(FaultReporter):
    def __init__(self) -> None:
        self.recorded: list[Any] = []

    @override
    def record(self, fault: Any) -> None:
        self.recorded.append(fault)


class Responder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)


class ToolStartingWhole:
    def __init__(
        self,
        hook: Callable[[], None],
        lifecycle: TurnLifecycle,
        outcomes: list[BaseException | None],
    ) -> None:
        self._hook = hook
        self._lifecycle = lifecycle
        self._outcomes = outcomes

    async def respond(self, _transcript: str) -> SpokenChunk:
        self._record(self._hook)
        self._lifecycle.fail(RuntimeError("boom"))
        self._record(self._hook)
        return SpokenChunk(text="Yes, I am here.", language=DEFAULTS.speech.language)

    def _record(self, hook: Callable[[], None]) -> None:
        try:
            hook()
        except ConversationError as error:
            self._outcomes.append(error)
        else:
            self._outcomes.append(None)


class FakeDecider:
    def __init__(self) -> None:
        self.calls: list[tuple[DeciderTurn, ...]] = []

    async def decide(self, window: Any) -> ParticipationDecision:
        self.calls.append(tuple(window))
        return ParticipationDecision(decision="silent", confidence=0.9, reason_code="model_says")


def transcript(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="Ana",
        speaker_evidence_id=None,
    )


def lifecycle_for(
    journal: Journal,
    *,
    faults: RecordingFaults | None = None,
    decider: FakeDecider | None = None,
    jev: JevState | None = None,
) -> TurnLifecycle:
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=decider, window_turns=6),
        journal=journal,
        responder=Responder(),
        jev=jev,
    )
    if faults is not None:
        lifecycle.configure_recovery(RecoveryOptions(faults=faults))
    return lifecycle


async def hold_speech(lifecycle: TurnLifecycle, release: asyncio.Event) -> asyncio.Task[bool]:
    task = asyncio.create_task(lifecycle.floor.hold(wait_until(release)))
    await asyncio.sleep(0)
    assert lifecycle.floor.busy
    return task


def test_lifecycle_fail_records_through_fault_reporter_with_the_literal_call() -> None:
    assert 'self._options.faults.record(classify_exception("agent", cause))' in LIFECYCLE_SOURCE


def test_tool_start_raises_after_the_acting_move() -> None:
    assert '_move_phase(runtime, "acting", detail)\n            _raise_if_failed(runtime)' in (
        GRAPH_SOURCE
    )


def test_wait_idle_shields_the_drain_then_raises_the_failure() -> None:
    assert "await asyncio.shield(drain)" in LIFECYCLE_SOURCE
    assert "await asyncio.shield(active.task)\n        self.raise_if_failed()" in LIFECYCLE_SOURCE


def test_barge_in_and_jev_kick_read_the_session_speaking_probe() -> None:
    assert "if runtime.barge_in and speaker.is_speaking:" in GRAPH_SOURCE
    assert "busy=_busy(runtime) or runtime.speaker.is_speaking," in GRAPH_SOURCE


async def test_fail_records_the_first_fault_through_the_reporter_only_once() -> None:
    faults = RecordingFaults()
    lifecycle = lifecycle_for(Journal(), faults=faults)

    lifecycle.fail(RuntimeError("first"))
    lifecycle.fail(RuntimeError("second"))

    assert len(faults.recorded) == 1
    fault = faults.recorded[0]
    assert isinstance(fault, ProviderFault)
    assert fault.source == "agent"
    assert fault.kind == "unknown"
    assert lifecycle.is_failed


async def test_raise_if_failed_after_a_tool_start_raises_the_failure_and_moves_acting() -> None:
    lifecycle = lifecycle_for(Journal())
    moves: list[str] = []
    lifecycle.observe(lambda _previous, phase, _detail: moves.append(phase))
    outcomes: list[BaseException | None] = []
    lifecycle.configure_reply(
        ReplyOptions(
            whole_turn=lambda _events, hook: ToolStartingWhole(hook, lifecycle, outcomes),
            sentences=SentenceLimits(max_sentences=2, language="en"),
        )
    )
    lifecycle.bind(Session())

    await TurnIngress(lifecycle, addressing_names=("twin",)).handle_final(
        transcript("f1", "twin, hi")
    )

    assert outcomes[0] is None
    assert isinstance(outcomes[1], ConversationError)
    assert str(outcomes[1]) == "meeting conversation failed"
    assert isinstance(outcomes[1].__cause__, RuntimeError)
    assert moves.count("acting") == 2


async def test_wait_idle_returns_after_the_held_floor_is_released() -> None:
    lifecycle = lifecycle_for(Journal())
    release = asyncio.Event()
    held = await hold_speech(lifecycle, release)

    waiter = asyncio.create_task(lifecycle.wait_idle())
    await asyncio.sleep(0)
    assert not waiter.done()

    release.set()
    await waiter
    assert await held is True
    assert not lifecycle.floor.busy


async def test_wait_idle_raises_the_failure_after_the_floor_is_idle() -> None:
    lifecycle = lifecycle_for(Journal())
    lifecycle.fail(RuntimeError("late"))

    with pytest.raises(ConversationError, match="meeting conversation failed"):
        await lifecycle.wait_idle()


async def test_close_releases_the_held_floor_and_journals_the_cancel() -> None:
    journal = Journal()
    lifecycle = lifecycle_for(journal)
    lifecycle.bind(Session())
    release = asyncio.Event()
    held = await hold_speech(lifecycle, release)

    await lifecycle.close()

    assert await held is False
    assert not lifecycle.floor.busy
    assert lifecycle.floor.last_reason == "close"
    assert journal.names() == ["agent_cancel_requested", "agent_cancelled"]


async def test_final_barge_in_follows_the_speaker_probe_not_the_floor() -> None:
    journal = Journal()
    lifecycle = lifecycle_for(journal)
    session = Session()
    lifecycle.bind(session)
    release = asyncio.Event()
    await hold_speech(lifecycle, release)

    await TurnIngress(lifecycle, addressing_names=("twin",)).handle_final(
        transcript("f1", "twin, hi")
    )

    assert journal.of("barge_in_detected") == []
    assert session.stops == 0


async def test_final_barge_in_fires_when_the_speaker_is_speaking() -> None:
    journal = Journal()
    lifecycle = lifecycle_for(journal)
    session = Session()
    lifecycle.bind(session)
    release = asyncio.Event()
    await hold_speech(lifecycle, release)
    session.is_speaking = True

    await TurnIngress(lifecycle, addressing_names=("twin",)).handle_final(
        transcript("f1", "twin, hi")
    )

    assert journal.of("barge_in_detected") == [{"source": "agent", "trigger": "final", "words": 2}]
    assert session.stops == 1


async def test_jev_kick_is_skipped_while_the_speaker_is_speaking() -> None:
    decider = FakeDecider()
    jev = JevState()
    lifecycle = lifecycle_for(Journal(), decider=decider, jev=jev)
    session = Session()
    lifecycle.bind(session)
    ingress = TurnIngress(lifecycle, addressing_names=("twin",))
    await ingress.handle_final(transcript("f1", "good morning everyone"))

    session.is_speaking = True
    await ingress.handle_interim("twin, what do you think")
    await jev.wait_idle()

    assert decider.calls == []
    assert jev.task is None


async def test_jev_kick_runs_when_the_speaker_is_silent() -> None:
    decider = FakeDecider()
    jev = JevState()
    lifecycle = lifecycle_for(Journal(), decider=decider, jev=jev)
    lifecycle.bind(Session())
    ingress = TurnIngress(lifecycle, addressing_names=("twin",))
    await ingress.handle_final(transcript("f1", "good morning everyone"))

    await ingress.handle_interim("twin, what do you think")
    await jev.wait_idle()

    assert len(decider.calls) == 1
