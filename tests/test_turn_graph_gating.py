import asyncio
from collections.abc import AsyncIterator
from typing import Any, override

import pytest

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.meeting_messages import NoHistory
from fakeyou.conversation.participation_graph import JudgeThresholds, TurnJudging
from fakeyou.conversation.reply_planner import SpokenChunk
from fakeyou.conversation.turn_events import TurnEvent
from fakeyou.conversation.turn_graph import (
    ConversationPhase,
    TurnInput,
    TurnRuntime,
    build_turn_graph,
)
from fakeyou.conversation.turn_ingress import TurnIngress
from fakeyou.conversation.turn_judge import (
    SPEAKING_NOW,
    JudgeCache,
    JudgeLine,
    JudgeWindow,
    TurnJudgement,
    WindowSource,
)
from fakeyou.conversation.turn_lifecycle import PublicPhase, TurnLifecycle, public_phase
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.transcript import TranscriptEvent
from tests._audio_fakes import SilentSpeaker
from tests._conversation_fakes import judged
from tests._prepared_support import PreparedSpeaker

TWIN_REPLY = "Yes, I am here."
POLICY_FIELDS = {
    "heard",
    "transcript_id",
    "action",
    "reason",
    "decision_source",
    "confidence",
    "addressee",
    "finished",
    "backchannel",
    "source",
    "requests_chat",
    "meet_action",
    "evidence_needs",
    "reaction_emoji",
    "action_only",
    "logistics_check",
    "state_question",
}


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


class FakeResponder:
    async def stream_reply(self, _transcript: str) -> AsyncIterator[SpokenChunk]:
        yield SpokenChunk(text=TWIN_REPLY, language=DEFAULTS.speech.language)


class GatedJudge:
    def __init__(
        self,
        judgement: TurnJudgement | None = None,
        *,
        error: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._judgement = judgement or judged("stay_silent")
        self._error = error
        self._gate = gate
        self.calls: list[JudgeWindow] = []
        self.cancelled = 0

    async def judge(self, window: JudgeWindow) -> TurnJudgement:
        self.calls.append(window)
        try:
            if self._gate is not None:
                await self._gate.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self._error is not None:
            raise self._error
        return self._judgement


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


def judging(judge: GatedJudge) -> TurnJudging:
    return TurnJudging(
        cache=JudgeCache(judge),
        windows=WindowSource(NoHistory(), window_lines=8),
        thresholds=JudgeThresholds(reply=0.6, clarify=0.5),
    )


def interim_input(text: str, *, pending: bool = False) -> TurnInput:
    return TurnInput(
        event=TurnEvent(kind="interim", event_id="i1", at=1.0, payload=text),
        context=ParticipationContext(addressed=False),
        speaking_generation=0,
        addressing_names=("twin",),
        pending=pending,
    )


def bound(
    judge: GatedJudge,
    journal: FakeJournal,
    *,
    on_transition: Any = None,
) -> tuple[TurnLifecycle, FakeSession]:
    lifecycle = TurnLifecycle(
        build_turn_graph(),
        journal=journal,
        responder=FakeResponder(),
        judging=judging(judge),
    )
    if on_transition is not None:
        lifecycle.observe(on_transition)
    session = FakeSession()
    lifecycle.bind(session)
    return lifecycle, session


def ingress_for(lifecycle: TurnLifecycle) -> TurnIngress:
    return TurnIngress(lifecycle, addressing_names=("twin",))


async def settle(lifecycle: TurnLifecycle) -> None:
    judging = lifecycle.judging
    assert judging is not None
    await judging.cache.wait_idle()


async def test_every_interim_is_judged_in_the_background() -> None:
    judge = GatedJudge()
    lifecycle, _ = bound(judge, FakeJournal())

    await ingress_for(lifecycle).handle_interim("nice weather today")
    await settle(lifecycle)

    assert [window.latest.text for window in judge.calls] == ["nice weather today"]


async def test_interim_is_not_judged_while_a_final_is_in_flight() -> None:
    judge = GatedJudge()
    cache = judging(judge)

    await build_turn_graph().run(
        interim_input("twin, what do you think", pending=True),
        context=TurnRuntime(
            speaker=SilentSpeaker(), journal=FakeJournal(), responder=FakeResponder(), judging=cache
        ),
    )
    await cache.cache.wait_idle()

    assert judge.calls == []


async def test_the_judge_is_asked_about_the_window_the_ingress_built() -> None:
    judge = GatedJudge()
    cache = judging(judge)
    window = JudgeWindow((), JudgeLine(SPEAKING_NOW, "twin, hello"), twin_speaking=True)

    await build_turn_graph().run(
        TurnInput(
            event=TurnEvent(kind="interim", event_id="i1", at=1.0, payload="twin, hello"),
            context=ParticipationContext(addressed=False),
            speaking_generation=0,
            addressing_names=("twin",),
            judge_window=window,
        ),
        context=TurnRuntime(
            speaker=SilentSpeaker(), journal=FakeJournal(), responder=FakeResponder(), judging=cache
        ),
    )
    await cache.cache.wait_idle()

    assert len(judge.calls) == 1
    assert judge.calls[0] is window


async def test_repeated_interim_text_while_a_judgement_runs_is_deduped() -> None:
    gate = asyncio.Event()
    judge = GatedJudge(gate=gate)
    lifecycle, _ = bound(judge, FakeJournal())
    ingress = ingress_for(lifecycle)

    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    gate.set()
    await settle(lifecycle)

    assert len(judge.calls) == 1


async def test_new_interim_text_keeps_the_running_judgement() -> None:
    gate = asyncio.Event()
    judge = GatedJudge(gate=gate)
    lifecycle, _ = bound(judge, FakeJournal())
    ingress = ingress_for(lifecycle)

    await ingress.handle_interim("twin, hello")
    await asyncio.sleep(0)
    await ingress.handle_interim("twin, hello again")
    await asyncio.sleep(0)
    gate.set()
    await settle(lifecycle)

    assert judge.cancelled == 0
    assert len(judge.calls) == 2


async def test_lifecycle_close_cancels_the_running_judgement() -> None:
    gate = asyncio.Event()
    judge = GatedJudge(gate=gate)
    lifecycle, _ = bound(judge, FakeJournal())

    await ingress_for(lifecycle).handle_interim("twin, hello")
    await asyncio.sleep(0)
    await lifecycle.close()
    await asyncio.sleep(0)

    assert judge.cancelled == 1


async def test_the_final_reuses_the_interim_judgement() -> None:
    judge = GatedJudge(judged("reply"))
    journal = FakeJournal()
    lifecycle, session = bound(judge, journal)
    ingress = ingress_for(lifecycle)

    await ingress.handle_interim("what about the beta")
    await settle(lifecycle)
    await ingress.handle_final(transcript("f2", "What about the beta?"))

    assert len(judge.calls) == 1
    assert session.spoken == [TWIN_REPLY]
    last = journal.of("policy_decided")[-1]
    assert (last["decision_source"], last["reason"], last["action"]) == (
        "judge",
        "judge_reply",
        "reply",
    )


async def test_a_final_without_an_interim_is_judged_on_arrival() -> None:
    judge = GatedJudge(judged("reply"))
    lifecycle, session = bound(judge, FakeJournal())

    await ingress_for(lifecycle).handle_final(transcript("f1", "What about the beta?"))

    assert [window.latest.text for window in judge.calls] == ["What about the beta?"]
    assert session.spoken == [TWIN_REPLY]


async def test_a_failed_judgement_is_journaled_with_its_code_and_falls_back() -> None:
    journal = FakeJournal()
    lifecycle, session = bound(GatedJudge(error=RuntimeError("upstream down")), journal)

    await ingress_for(lifecycle).handle_final(transcript("f2", "What about the beta?"))

    assert journal.of("policy_judge_failed")[-1]["code"] == "judge_unavailable"
    last = journal.of("policy_decided")[-1]
    assert (last["reason"], last["decision_source"]) == ("fallback_unnamed", "fallback")
    assert session.spoken == []
    assert "turn_failed" not in journal.names()


async def test_a_reply_below_the_threshold_stays_silent() -> None:
    journal = FakeJournal()
    lifecycle, session = bound(GatedJudge(judged("reply", confidence=0.5)), journal)

    await ingress_for(lifecycle).handle_final(transcript("f2", "What about the beta?"))

    last = journal.of("policy_decided")[-1]
    assert (last["reason"], last["action"], last["confidence"]) == (
        "judge_below_threshold",
        "silent",
        0.5,
    )
    assert session.spoken == []


async def test_a_clarify_is_admitted_and_speaks() -> None:
    journal = FakeJournal()
    lifecycle, session = bound(GatedJudge(judged("clarify")), journal)

    await ingress_for(lifecycle).handle_final(transcript("f2", "the thing, you know?"))

    last = journal.of("policy_decided")[-1]
    assert (last["reason"], last["action"]) == ("judge_clarify", "clarify")
    assert session.spoken == [TWIN_REPLY]


async def test_the_policy_record_carries_the_judgement_fields() -> None:
    journal = FakeJournal()
    lifecycle, _ = bound(GatedJudge(judged("reply")), journal)

    await ingress_for(lifecycle).handle_final(transcript("f1", "twin, are you there?"))

    records = journal.of("policy_decided")
    assert len(records) == 1
    record = records[0]
    assert set(record) == POLICY_FIELDS
    assert (record["source"], record["transcript_id"], record["action"]) == ("agent", "f1", "reply")
    assert (record["addressee"], record["finished"], record["backchannel"]) == ("twin", True, False)


async def test_an_admitted_final_moves_listening_to_weighing() -> None:
    transitions: list[tuple[str, str, str]] = []
    lifecycle, _ = bound(
        GatedJudge(judged("reply")),
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
        GatedJudge(judged("reply")),
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
        GatedJudge(),
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
        GatedJudge(),
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
        GatedJudge(),
        FakeJournal(),
        on_transition=lambda old, new, detail: transitions.append((old, new, detail)),
    )

    lifecycle.fail(RuntimeError("boom"))

    assert lifecycle.phase == "failed"
    assert public_phase(lifecycle.phase) == "failed"
    assert transitions == []
