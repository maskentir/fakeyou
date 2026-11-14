import ast
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import override

import pytest

from fakeyou.audio.audio import DEFAULT_STREAM_COMPONENTS, StreamCallbacks
from fakeyou.audio.playback import FRAME_BYTES, PlaybackEngine, PlaybackEvent
from fakeyou.audio.stream_capture import (
    DEFAULT_SLOT_FACTORIES,
    FleetTopology,
    SlotFactories,
    open_stream_fleet,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.conversation.actor import (
    ActorWiring,
    DeferredOptions,
    FloorActor,
    ReplyOptions,
)
from fakeyou.conversation.model_responder import ReplierWholeResponder
from fakeyou.conversation.state_speech import render
from fakeyou.conversation.turn_graph import (
    PlannedTurn,
    SentenceLimits,
    StateAnswer,
    build_turn_graph,
)
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC, ClockExchange, RemoteClock
from fakeyou.foundation.publication import (
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
    sentence_digest,
)
from fakeyou.foundation.transcript import TranscriptEvent, TranscriptUpdate
from fakeyou.observability.participant_audio import measure_participant_publication
from fakeyou.orchestration.kernel_port import KernelPort
from fakeyou.orchestration.publication_gate import (
    GateSources,
    compose_publication_gate,
    opt_in_publication_context,
)
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import MeetingEvidence, PerceptionContext
from fakeyou.perception.inspector import VisualInspection, VisualVerdict
from fakeyou.perception.interpreter import PerceptionInterpreter
from fakeyou.perception.timeline import CameraSample, MeetingTimeline
from fakeyou.session.contracts import Observation
from fakeyou.session.facts import StateQuestion, state_fact
from fakeyou.session.intent import EvidenceNeed, Reading
from fakeyou.session.planner import TurnPlan
from fakeyou.session.policy import Channel
from fakeyou.session.state_publication import state_provenance
from fakeyou.session.world import SessionState
from fakeyou.speech.contract import PlaybackReceipt, PreparedSpeech, ReceiptObserver
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._capture_support import echo_stt
from tests._model_responder import ModelResponder
from tests._perception_fakes import START, keyframe, reading, slide_jpeg, vision_model
from tests._persistence_fakes import RecordingJournal
from tests.test_assembly_state_speech import observe, sharing_kernel
from tests.test_camera_evidence import jpeg_of
from tests.test_state_publication import AYU_SHARING, BUDI_TAKES_OVER
from tests.test_turn_graph_whole_adapter import (
    FakeJournal,
    FakeReplier,
    FakeSession,
    ToolPhaseState,
    transcript,
    unused_chunks,
)

CAPTURED_AT = 100.0
ROADMAP = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])
BUDGET = slide_jpeg("Budget", ["Total: 4 million"], background=(90, 20, 20))


class Clock:
    def __init__(self) -> None:
        self.at = CAPTURED_AT

    def __call__(self) -> float:
        return self.at


@dataclass
class Meeting(FakeSession):
    inspections: tuple[VisualInspection, ...] = (
        VisualInspection(verdict="supported", answer="Beta is 17 March 2027."),
    )
    mapped: bool = True
    gated: bool = True
    look_at: float = CAPTURED_AT + 1.0
    write_at: float = CAPTURED_AT + 2.0
    before_write: Callable[["Meeting"], None] | None = None
    clock: Clock = field(default_factory=Clock)
    kernel: KernelPort = field(default_factory=sharing_kernel)
    events: list[PlaybackEvent] = field(default_factory=list)
    receipts: list[PlaybackReceipt] = field(default_factory=list)
    bindings: list[SentenceBinding] = field(default_factory=list)
    writer: FakeWriter = field(default_factory=FakeWriter)
    journal: RecordingJournal = field(default_factory=RecordingJournal)
    page_clock: RemoteClock = field(init=False)
    timeline: MeetingTimeline = field(init=False)
    perception: PerceptionContext = field(init=False)
    engine: PlaybackEngine = field(init=False)
    publication_context: PublicationContext | None = field(init=False)

    def __post_init__(self) -> None:
        FakeSession.__init__(self)
        self.page_clock = RemoteClock(now=self.clock, drift_ppm=0)
        self.timeline = MeetingTimeline()
        presenter = self.timeline.participant("device-ayu", "Ayu")
        self.timeline.participant("device-budi", "Budi")
        self.timeline.show_screen(keyframe(ROADMAP, seconds=0), presenter, data=ROADMAP)
        self.timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March"))
        self.timeline.camera_frames.put(
            "device-budi",
            CameraSample(
                data=jpeg_of((120, 120, 120)),
                stream_id="stream-budi",
                captured_at=START,
                epoch=0,
                seen_at=CAPTURED_AT,
            ),
        )
        interpreter = PerceptionInterpreter(
            vision_model(inspections=self.inspections),
            PerceptionBudget(budget_usd=1.0, max_calls=10),
            max_concurrent=1,
            snapshots=self.timeline.camera_frames,
            monotonic=self.clock,
            provider="fake/vision",
        )
        self.perception = PerceptionContext(
            self.timeline,
            interpreter,
            max_chars=6000,
            clock=lambda: START,
            camera_monotonic=self.clock,
        )
        if self.mapped:
            self.perception.capture_clock = lambda captured_at: self.page_clock.map(
                captured_at.timestamp() * 1000
            )
        ids = (f"publication-{number}" for number in range(1, 100))
        self.publication_context = (
            opt_in_publication_context(
                "meeting-9",
                perception=self.perception,
                kernel=self.kernel,
                now=self.clock,
                id_factory=lambda: next(ids),
            )
            if self.gated
            else None
        )
        self.engine = build_engine(
            FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), self.writer, self.events
        )
        self.engine.configure_publication(self.publication_context)
        self.engine.observe_receipts(self.receipts.append)

    def advance(self, to: float) -> None:
        self.clock.at = to
        page_ms = (START + timedelta(seconds=to - CAPTURED_AT)).timestamp() * 1000
        assert self.page_clock.observe(ClockExchange(page_ms, to - 0.01, to))

    def observe(self, *observations: Observation) -> None:
        observe(self.kernel, *observations)

    @property
    def state(self) -> SessionState:
        return self.kernel.state

    def observe_receipts(self, observer: ReceiptObserver) -> None:
        self.engine.observe_receipts(observer)

    async def evidence(
        self, question: str, needs: tuple[str, ...], transcript_id: str | None = None
    ) -> Mapping[str, object]:
        self.advance(self.look_at)
        return await MeetingEvidence(self.perception, journal=self.journal)(
            question, needs, transcript_id
        )

    def state_speech(self, question: StateQuestion, _language: str | None) -> StateAnswer:
        fact = state_fact(question, self.state)
        return StateAnswer(
            render(fact.proposition, "en"), state_provenance(question, fact, self.state)
        )

    @override
    async def prepare(
        self,
        text: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech:
        prepared = await self.engine.prepare(
            text, sentence_index=sentence_index, transcript_id=transcript_id, language=language
        )
        assert prepared is not None
        return prepared

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None:
        self.bindings.append(binding)
        self.engine.bind_publication(prepared, binding)

    @override
    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        self.advance(self.write_at)
        if self.before_write is not None:
            self.before_write(self)
        await self.engine.speak_prepared(prepared)

    @override
    async def speak(self, text: str, **_: object) -> None:
        await self.engine.speak(text)

    async def speak_no_visual(
        self, text: str, *, transcript_id: str | None = None, language: str | None = None
    ) -> None:
        await self.engine.speak_no_visual(text, transcript_id=transcript_id, language=language)

    @property
    def settled(self) -> list[PlaybackReceipt]:
        return [item for item in self.receipts if item.settled]

    @property
    def prevented(self) -> str:
        [receipt] = self.settled
        assert receipt.outcome == "prevented"
        assert self.writer.written == []
        return receipt.detail.removeprefix("publication_prevented:")


@dataclass
class Turn:
    meeting: Meeting
    counted: list[PlaybackReceipt]


async def ask(
    meeting: Meeting,
    question: str,
    *,
    needs: tuple[EvidenceNeed, ...] = (),
    state_question: StateQuestion | None = None,
    reply: str = "Beta is on 17 March 2027.",
) -> Turn:
    def planning(event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
        del reading, channel
        if state_question is not None:
            return PlannedTurn(
                TurnPlan(
                    f"plan-{event.id}",
                    "speech_only",
                    output="speech",
                    state_question=state_question,
                )
            )
        if needs:
            return PlannedTurn(
                TurnPlan(f"plan-{event.id}", "evidence_then_speech", "speech", needs)
            )
        return PlannedTurn(TurnPlan(f"plan-{event.id}", "speech_only", "speech"))

    replier = FakeReplier(reply=reply)
    actor = FloorActor(
        build_turn_graph(),
        ActorWiring(
            journal=FakeJournal(),
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        ),
    )
    actor.configure_reply(
        ReplyOptions(
            whole_turn=lambda events, hook: ReplierWholeResponder(replier, events, hook),
            sentences=SentenceLimits(max_sentences=2, language="en"),
        )
    )
    counted: list[PlaybackReceipt] = []
    actor.observe_spoken(counted.append)
    actor.bind(meeting)
    actor.configure_deferred(
        DeferredOptions(
            planning=planning, evidence=meeting.evidence, state_speech=meeting.state_speech
        )
    )
    try:
        await actor.handle_final(transcript("f1", question))
    finally:
        await actor.close()
        await meeting.engine.close()
    return Turn(meeting, counted)


async def test_fresh_supported_screen_claim_is_published_and_counted() -> None:
    turn = await ask(Meeting(), "When is beta?", needs=("screen",))

    [binding] = turn.meeting.bindings
    [evidence] = binding.evidence
    assert binding.scope == "current"
    assert evidence.capture_monotonic == pytest.approx(CAPTURED_AT, abs=0.01)
    assert evidence.provider == "fake/vision"
    assert evidence.status == "produced"
    assert turn.meeting.writer.written
    assert [item.outcome for item in turn.meeting.settled] == ["completed"]
    assert [item.publication_id for item in turn.counted] == [binding.publication_id]


async def test_published_claim_without_a_participant_observer_is_never_compliant() -> None:
    turn = await ask(Meeting(), "When is beta?", needs=("screen",))
    [binding] = turn.meeting.bindings
    [receipt] = turn.meeting.settled

    result = measure_participant_publication(
        binding,
        None,
        decision=PublicationDecision("valid", "ok", turn.meeting.write_at, LOCAL_MONOTONIC),
        clock_domain=LOCAL_MONOTONIC,
        receipt=receipt,
    )

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_observer_unavailable",
    )


@pytest.mark.parametrize("age", [5.5, 30.0, 300.0])
async def test_finding_static_slide_older_than_five_seconds_is_prevented(age: float) -> None:
    meeting = Meeting(look_at=CAPTURED_AT + age - 1.0, write_at=CAPTURED_AT + age)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    expected = "unknown:capture_time_unmapped" if age > 120 else "invalid:capture_too_old"
    assert turn.meeting.prevented == expected
    assert turn.counted == []


async def test_finding_reply_slower_than_the_capture_limit_is_prevented() -> None:
    turn = await ask(
        Meeting(look_at=CAPTURED_AT + 0.5, write_at=CAPTURED_AT + 5.2),
        "When is beta?",
        needs=("screen",),
    )

    assert turn.meeting.prevented == "invalid:capture_too_old"


@pytest.mark.parametrize("verdict", ["not_visible", "unclear"])
async def test_finding_reply_after_an_abstaining_look_is_prevented(
    verdict: VisualVerdict,
) -> None:
    meeting = Meeting(inspections=(VisualInspection(verdict=verdict),))

    turn = await ask(
        meeting, "When is beta?", needs=("screen",), reply="I cannot see that on the screen."
    )

    [binding] = turn.meeting.bindings
    assert binding.evidence[0].status == "insufficient"
    assert turn.meeting.prevented == "unknown:validation_support_unknown"
    assert turn.counted == []


def show_budget(meeting: Meeting) -> None:
    presenter = meeting.timeline.participant("device-ayu", "Ayu")
    meeting.timeline.show_screen(keyframe(BUDGET, seconds=1.5), presenter, data=BUDGET)


def stop_sharing(meeting: Meeting) -> None:
    meeting.timeline.stop_screen(START + timedelta(seconds=1.5))


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (show_budget, "invalid:subject_changed"),
        (stop_sharing, "unknown:scene_continuity_unknown"),
    ],
)
async def test_screen_change_between_the_look_and_the_write_prevents_the_claim(
    change: Callable[[Meeting], None], reason: str
) -> None:
    turn = await ask(Meeting(before_write=change), "When is beta?", needs=("screen",))

    assert turn.meeting.prevented == reason
    assert turn.counted == []


async def test_unmapped_capture_clock_prevents_the_claim() -> None:
    turn = await ask(Meeting(mapped=False), "When is beta?", needs=("screen",))

    assert turn.meeting.prevented == "unknown:capture_time_unmapped"


async def test_fresh_camera_claim_is_published() -> None:
    meeting = Meeting(inspections=(VisualInspection(verdict="supported", answer="A grey shirt."),))

    turn = await ask(
        meeting, "What is Budi wearing?", needs=("participant",), reply="Budi wears grey."
    )

    [binding] = turn.meeting.bindings
    assert binding.evidence[0].kind == "participant"
    assert turn.meeting.writer.written
    assert len(turn.counted) == 1


def camera_goes_stale(meeting: Meeting) -> None:
    meeting.timeline.camera_frames.drop("device-budi")


async def test_camera_that_went_away_before_the_write_prevents_the_claim() -> None:
    meeting = Meeting(
        inspections=(VisualInspection(verdict="supported", answer="A grey shirt."),),
        before_write=camera_goes_stale,
    )

    turn = await ask(
        meeting, "What is Budi wearing?", needs=("participant",), reply="Budi wears grey."
    )

    assert turn.meeting.prevented == "unknown:scene_continuity_unknown"


async def test_reply_without_visual_needs_is_published_under_the_composed_gate() -> None:
    turn = await ask(Meeting(), "How are you?", reply="I am well.")

    [binding] = turn.meeting.bindings
    assert binding.scope == "no_visual"
    assert turn.meeting.writer.written
    assert len(turn.counted) == 1


async def test_state_answer_is_validated_by_the_composed_gate() -> None:
    turn = await ask(Meeting(), "Who is presenting?", state_question="presenter")

    [binding] = turn.meeting.bindings
    assert binding.scope == "state"
    assert turn.meeting.writer.written
    assert len(turn.counted) == 1


async def test_state_answer_is_prevented_when_the_presenter_changed() -> None:
    meeting = Meeting(before_write=lambda item: item.observe(BUDI_TAKES_OVER))

    turn = await ask(meeting, "Who is presenting?", state_question="presenter")

    assert turn.meeting.prevented == "invalid:state_subject_changed"
    assert turn.counted == []


async def test_declared_no_visual_greeting_is_published_under_the_composed_gate() -> None:
    meeting = Meeting()
    try:
        await meeting.speak_no_visual("Hi everyone, I just joined.")
        assert meeting.writer.written
        assert [item.outcome for item in meeting.settled] == ["completed"]
    finally:
        await meeting.engine.close()


def binding_of(scope: str) -> SentenceBinding:
    if scope == "state":
        fact = state_fact("presenter", AYU_SHARING)
        provenance = state_provenance("presenter", fact, AYU_SHARING)
        return SentenceBinding(
            "pub", "m", "t", "t", "r", 0, sentence_digest("x"), None, (), (), "state", provenance
        )
    return SentenceBinding("pub", "m", "t", "t", "r", 0, sentence_digest("x"), None, (), (), scope)


@pytest.mark.parametrize("scope", ["no_visual", "state"])
async def test_composed_gate_without_sources_only_allows_no_visual(scope: str) -> None:
    gate = compose_publication_gate(GateSources(), now=lambda: 1.0)

    decision = await gate(binding_of(scope))

    assert (decision is not None and decision.allows) is (scope == "no_visual")


async def test_opt_in_context_uses_the_local_monotonic_domain_and_unique_ids() -> None:
    meeting = Meeting()
    context = opt_in_publication_context(
        "meeting-9", perception=meeting.perception, kernel=None, now=meeting.clock
    )

    assert context.clock_domain == LOCAL_MONOTONIC
    assert context.gate is not None
    assert context.id_factory() != context.id_factory()
    await meeting.engine.close()
    await asyncio.sleep(0)


def test_publication_context_is_off_by_default_on_both_audio_paths() -> None:
    assert DEFAULT_STREAM_COMPONENTS.publication is None
    assert DEFAULT_SLOT_FACTORIES.publication is None


def test_no_production_module_composes_the_opt_in_gate() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "fakeyou"
    importers = []
    for path in root.rglob("*.py"):
        if path.name == "publication_gate.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                if node.module.endswith("orchestration.publication_gate"):
                    importers.append(str(path.relative_to(root)))
            elif isinstance(node, ast.Import):
                importers.extend(
                    str(path.relative_to(root))
                    for item in node.names
                    if item.name.endswith("orchestration.publication_gate")
                )
    assert importers == []


async def test_stream_fleet_carries_an_opt_in_publication_context_to_every_slot() -> None:
    context = PublicationContext("meeting-9", LOCAL_MONOTONIC, now=lambda: 1.0)

    async def on_transcript(_update: TranscriptUpdate) -> None:
        return None

    async with open_stream_fleet(
        topology=FleetTopology(SlotBinder(), 2),
        callbacks=StreamCallbacks(on_transcript),
        factories=SlotFactories(
            stt=lambda slot: echo_stt(f"speech-{slot}"),
            playback=lambda _slot: build_engine(FakeSynth(), FakeWriter(), []),
            publication=context,
        ),
    ) as fleet:
        assert fleet.publication_context is context
        prepared = await fleet.prepare("Hello.", sentence_index=0, transcript_id="t")
        assert prepared is not None
        fleet.bind_publication(
            prepared,
            SentenceBinding(
                "pub",
                "meeting-9",
                "t",
                "t",
                "r",
                0,
                sentence_digest("Hello."),
                None,
                (),
                (),
                "no_visual",
            ),
        )
        await fleet.speak_prepared(prepared)
