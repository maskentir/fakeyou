import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

import pytest

from fakeyou.conversation.jev_refresh import JevState
from fakeyou.conversation.participation_decider import DeciderTurn, ParticipationDecision
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    ConversationPhase,
    SpeechFloor,
    TurnInput,
    TurnRuntime,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_lifecycle import PublicPhase, TurnLifecycle, public_phase
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.transcript import TranscriptEvent
from tests._audio_fakes import SilentSpeaker

TWIN_REPLY = "Yes, I am here."
POLICY_FIELDS = {
    "transcript_id",
    "action",
    "reason",
    "decision_source",
    "confidence",
    "turn_cue",
    "source",
}


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

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def of(self, name: str) -> list[dict[str, Any]]:
        return [fields for event, fields in self.entries if event == name]


class FakeResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[str]:
        yield TWIN_REPLY


class FakeDecider:
    def __init__(
        self,
        decision: ParticipationDecision | None = None,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._decision = decision
        self._error = error
        self._gate = gate
        self.calls: list[tuple[DeciderTurn, ...]] = []
        self.cancelled = 0

    async def decide(self, window: Sequence[DeciderTurn]) -> ParticipationDecision:
        self.calls.append(tuple(window))
        try:
            if self._gate is not None:
                await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self._error is not None:
            raise self._error
        assert self._decision is not None
        return self._decision


def decision(
    kind: Literal["speak", "silent", "clarify"], confidence: float = 0.9
) -> ParticipationDecision:
    return ParticipationDecision(decision=kind, confidence=confidence, reason_code="model_says")


def transcript(event_id: str, text: str, participant_id: str = "Ana") -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-17T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


def interim_input(
    text: str,
    history: list[TranscriptEvent],
    *,
    pending: bool = False,
) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="interim", event_id="i1", at=1.0, payload=text),
        turns=[],
        context=ParticipationContext(
            addressed=False,
            followup=False,
            same_participant=False,
            ambiguous=False,
            others_speaking=False,
            activation_valid=False,
        ),
        speaking_generation=0,
        history=history,
        addressing_names=("twin",),
        pending=pending,
    )


def bound(
    decider: FakeDecider,
    journal: FakeJournal,
    *,
    jev: JevState | None = None,
    on_transition: Any = None,
) -> tuple[TurnLifecycle, FakeSession]:
    lifecycle = TurnLifecycle(
        build_turn_graph(decider=decider, window_turns=6),
        journal=journal,
        responder=FakeResponder(),
        jev=jev,
    )
    if on_transition is not None:
        lifecycle.observe(on_transition)
    session = FakeSession()
    lifecycle.bind(session)
    return lifecycle, session


def ingress_for(lifecycle: TurnLifecycle) -> TurnIngress:
    return TurnIngress(lifecycle, addressing_names=("twin",))


async def test_interim_with_a_turn_cue_consults_the_decider_in_the_background() -> None:
    decider = FakeDecider(decision("silent"))
    jev = JevState()
    lifecycle, _ = bound(decider, FakeJournal(), jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("twin, what do you think")
    await jev.wait_idle()

    assert len(decider.calls) == 1
    assert decider.calls[0][-1]["text"] == "good morning everyone"


async def test_interim_without_a_turn_cue_never_consults_the_decider() -> None:
    decider = FakeDecider(decision("silent"))
    jev = JevState()
    lifecycle, _ = bound(decider, FakeJournal(), jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("nice weather today")
    await jev.wait_idle()

    assert decider.calls == []
    assert jev.task is None


async def test_interim_is_suppressed_while_a_final_is_in_flight() -> None:
    decider = FakeDecider(decision("silent"))
    jev = JevState()
    graph = build_turn_graph(decider=decider, window_turns=6)
    history = [transcript("f1", "good morning everyone")]

    await graph.ainvoke(
        interim_input("twin, what do you think", history, pending=True),
        context=TurnRuntime(
            speaker=SilentSpeaker(), journal=FakeJournal(), responder=FakeResponder(), jev=jev
        ),
    )
    await jev.wait_idle()

    assert decider.calls == []
    assert jev.task is None


async def test_interim_is_suppressed_while_the_floor_is_busy() -> None:
    decider = FakeDecider(decision("silent"))
    jev = JevState()
    floor = SpeechFloor()
    graph = build_turn_graph(decider=decider, window_turns=6)
    speech = asyncio.create_task(floor.hold(asyncio.sleep(60)))
    await asyncio.sleep(0)

    await graph.ainvoke(
        interim_input("twin, what do you think", [transcript("f1", "good morning everyone")]),
        context=TurnRuntime(
            speaker=SilentSpeaker(),
            journal=FakeJournal(),
            responder=FakeResponder(),
            floor=floor,
            jev=jev,
        ),
    )
    await jev.wait_idle()
    speech.cancel()
    await asyncio.gather(speech, return_exceptions=True)

    assert decider.calls == []


async def test_repeated_interim_text_while_a_refresh_runs_is_deduped() -> None:
    gate = asyncio.Event()
    decider = FakeDecider(decision("silent"), gate=gate)
    jev = JevState()
    lifecycle, _ = bound(decider, FakeJournal(), jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    gate.set()
    await jev.wait_idle()

    assert len(decider.calls) == 1


async def test_new_interim_text_cancels_the_running_refresh() -> None:
    gate = asyncio.Event()
    decider = FakeDecider(decision("silent"), gate=gate)
    jev = JevState()
    lifecycle, _ = bound(decider, FakeJournal(), jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    await ingress.handle_interim("twin, hello again")
    await asyncio.sleep(0)
    gate.set()
    await jev.wait_idle()

    assert decider.cancelled == 1
    assert len(decider.calls) == 2


async def test_lifecycle_close_cancels_the_running_refresh() -> None:
    gate = asyncio.Event()
    decider = FakeDecider(decision("silent"), gate=gate)
    jev = JevState()
    lifecycle, _ = bound(decider, FakeJournal(), jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    await lifecycle.close()
    await asyncio.sleep(0)

    assert decider.cancelled == 1
    assert jev.task is None


async def test_final_reads_the_cached_decision_and_never_calls_the_decider() -> None:
    decider = FakeDecider(decision("speak", 0.9))
    jev = JevState()
    journal = FakeJournal()
    lifecycle, session = bound(decider, journal, jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("next")
    await jev.wait_idle()
    calls_after_refresh = len(decider.calls)
    await ingress.handle_final(transcript("f2", "next"))

    assert len(decider.calls) == calls_after_refresh == 1
    assert session.spoken == [TWIN_REPLY]
    last = journal.of("policy_decided")[-1]
    assert last["decision_source"] == "jev"
    assert last["reason"] == "jev_speak"
    assert last["action"] == "reply"
    assert last["turn_cue"] == "implicit"


async def test_final_without_a_cached_decision_falls_back_to_rules_and_journals_the_miss() -> None:
    decider = FakeDecider(decision("speak"))
    journal = FakeJournal()
    lifecycle, session = bound(decider, journal, jev=JevState())
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_final(transcript("f2", "next"))

    assert decider.calls == []
    assert session.spoken == []
    failure = journal.of("policy_decider_failed")[-1]
    assert failure["code"] == "decider_timeout"
    assert failure["decision_source"] == "rules"
    assert failure["source"] == "agent"
    last = journal.of("policy_decided")[-1]
    assert last["action"] == "silent"
    assert last["reason"] == "rules_fallback:not_addressed"
    assert last["decision_source"] == "rules"
    assert "turn_failed" not in journal.names()


async def test_cached_refresh_failure_is_journaled_with_its_code_on_the_final() -> None:
    decider = FakeDecider(error=RuntimeError("upstream down"))
    jev = JevState()
    journal = FakeJournal()
    lifecycle, session = bound(decider, journal, jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("next")
    await jev.wait_idle()
    await ingress.handle_final(transcript("f2", "next"))

    assert journal.of("policy_decider_failed")[-1]["code"] == "decider_unavailable"
    assert session.spoken == []


async def test_cached_speak_below_threshold_stays_silent() -> None:
    decider = FakeDecider(decision("speak", 0.6))
    jev = JevState()
    journal = FakeJournal()
    lifecycle, session = bound(decider, journal, jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("next")
    await jev.wait_idle()
    await ingress.handle_final(transcript("f2", "next"))

    last = journal.of("policy_decided")[-1]
    assert last["reason"] == "jev_below_threshold"
    assert last["decision_source"] == "jev"
    assert last["action"] == "silent"
    assert last["confidence"] == 0.6
    assert session.spoken == []


async def test_cached_clarify_is_admitted_and_speaks() -> None:
    decider = FakeDecider(decision("clarify", 0.9))
    jev = JevState()
    journal = FakeJournal()
    lifecycle, session = bound(decider, journal, jev=jev)
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))
    await ingress.handle_interim("next")
    await jev.wait_idle()
    await ingress.handle_final(transcript("f2", "next"))

    last = journal.of("policy_decided")[-1]
    assert last["reason"] == "jev_clarify"
    assert last["action"] == "clarify"
    assert session.spoken == [TWIN_REPLY]


async def test_addressed_final_is_journaled_on_the_rule_path_with_the_loop_fields() -> None:
    journal = FakeJournal()
    lifecycle, _ = bound(FakeDecider(decision("silent")), journal, jev=JevState())
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "twin, are you there?"))

    records = journal.of("policy_decided")
    assert len(records) == 1
    record = records[0]
    assert set(record) == POLICY_FIELDS
    assert record["source"] == "agent"
    assert record["transcript_id"] == "f1"
    assert record["action"] == "reply"
    assert record["reason"] == "addressed"
    assert record["decision_source"] == "rule"
    assert record["confidence"] == 1.0
    assert record["turn_cue"] == "explicit"


async def test_unaddressed_final_without_cue_is_journaled_as_a_rule_silence() -> None:
    journal = FakeJournal()
    lifecycle, session = bound(FakeDecider(decision("speak")), journal, jev=JevState())
    ingress = ingress_for(lifecycle)

    await ingress.handle_final(transcript("f1", "good morning everyone"))

    record = journal.of("policy_decided")[-1]
    assert record["action"] == "silent"
    assert record["reason"] == "not_addressed"
    assert record["decision_source"] == "rule"
    assert record["turn_cue"] == "none"
    assert session.spoken == []


async def test_addressed_final_moves_listening_to_weighing() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        FakeDecider(decision("silent")),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )

    await ingress_for(lifecycle).handle_final(transcript("f1", "twin, are you there?"))

    assert transitions[0] == ("listening", "weighing", "f1")
    assert [phase for _, phase, _ in transitions] == ["weighing", "queued", "listening"]
    assert lifecycle.phase == "listening"
    assert public_phase(lifecycle.phase) == "active"


async def test_final_while_speaking_moves_to_interrupted_before_weighing() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        FakeDecider(decision("silent")),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )
    speech = asyncio.create_task(lifecycle.floor.hold(asyncio.sleep(60)))
    await asyncio.sleep(0)

    await ingress_for(lifecycle).handle_final(transcript("f1", "twin, are you there?"))
    await asyncio.gather(speech, return_exceptions=True)

    assert transitions[:2] == [
        ("listening", "interrupted", "f1"),
        ("interrupted", "weighing", "f1"),
    ]
    assert [phase for _, phase, _ in transitions[2:]] == ["queued", "listening"]


async def test_silent_final_moves_no_phase() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        FakeDecider(decision("silent")),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )

    await ingress_for(lifecycle).handle_final(transcript("f1", "good morning everyone"))

    assert transitions == []
    assert lifecycle.phase == "listening"


@pytest.mark.parametrize(
    ("phase", "public"),
    [
        ("listening", "active"),
        ("weighing", "active"),
        ("queued", "active"),
        ("interrupted", "active"),
        ("waiting", "active"),
        ("acting", "active"),
        ("failed", "failed"),
        ("closed", "leaving"),
    ],
)
def test_conversation_phases_map_to_the_public_enum_per_phase_mapping(
    phase: ConversationPhase, public: PublicPhase
) -> None:
    assert public_phase(phase) == public


async def test_close_sets_closed_without_a_transition_callback() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        FakeDecider(decision("silent")),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )

    await lifecycle.close()

    assert lifecycle.phase == "closed"
    assert public_phase(lifecycle.phase) == "leaving"
    assert transitions == []


async def test_fail_sets_failed_without_a_transition_callback() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        FakeDecider(decision("silent")),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )

    lifecycle.fail(RuntimeError("boom"))

    assert lifecycle.phase == "failed"
    assert public_phase(lifecycle.phase) == "failed"
    assert transitions == []
