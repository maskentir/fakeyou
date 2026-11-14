import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace

import pytest

from fakeyou.audio.playback import FRAME_BYTES, PlaybackEngine, PlaybackEvent
from fakeyou.conversation import turn_graph
from fakeyou.conversation.actor import ActorWiring, DeferredOptions, FloorActor
from fakeyou.conversation.state_speech import render
from fakeyou.conversation.turn_graph import PlannedTurn, StateAnswer, build_turn_graph
from fakeyou.foundation.publication import (
    AudioPublication,
    EvidenceBinding,
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
    StateProvenance,
    Validation,
    evaluate_visual_publication,
    sentence_digest,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.observability.publication_measurement import measure_publication
from fakeyou.session.contracts import (
    Endpoint,
    Observation,
    RosterObserved,
    ScreenObserved,
    SourceHealthObserved,
)
from fakeyou.session.facts import StateQuestion, state_fact
from fakeyou.session.intent import Reading
from fakeyou.session.planner import TurnPlan
from fakeyou.session.policy import Channel
from fakeyou.session.state_publication import (
    CURRENT_STATE_QUESTIONS,
    latest_state_provenance,
    state_provenance,
    state_publication_decision,
)
from fakeyou.session.world import SessionState
from fakeyou.speech.contract import PreparedSpeech
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._model_responder import ModelResponder
from tests._persistence_fakes import RecordingJournal
from tests._turn_context import TurnTestContext
from tests.test_facts import AYU, AYU_SCREEN, BUDI, IN_CALL, TWIN, replay
from tests.test_turn_graph_whole_adapter import (
    FakeJournal,
    ToolPhaseState,
    transcript,
    unused_chunks,
)

BUDI_SCREEN = Endpoint(
    device_id="dev-budi-screen", display_name="Budi", present=True, parent_device_id="dev-budi"
)
AYU_SHARING = replay(
    IN_CALL,
    RosterObserved(endpoints=(TWIN, AYU, BUDI, AYU_SCREEN), synced=True),
    ScreenObserved(title="Q4 roadmap"),
)
AYU_SCREEN_GONE = AYU_SCREEN.model_copy(update={"present": False})
BUDI_TAKES_OVER = RosterObserved(
    endpoints=(TWIN, AYU, BUDI, AYU_SCREEN_GONE, BUDI_SCREEN),
    synced=True,
)
SHARING_STOPS = RosterObserved(endpoints=(TWIN, AYU, BUDI, AYU_SCREEN_GONE), synced=True)
PROTOCOL_STALE = SourceHealthObserved(source="meet_protocol", status="stale")


@dataclass
class Meeting:
    state: SessionState = AYU_SHARING
    events: list[PlaybackEvent] = field(default_factory=list)
    writer: FakeWriter = field(default_factory=FakeWriter)
    bindings: list[SentenceBinding] = field(default_factory=list)
    before_playback: Callable[[], None] | None = None
    gated: bool = True
    engine: PlaybackEngine = field(init=False)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.engine = build_engine(
            FakeSynth(audio=b"\x01\x00" * FRAME_BYTES), self.writer, self.events
        )
        ids = (f"publication-{number}" for number in range(1, 100))
        self.publication_context = PublicationContext(
            "meeting-9",
            "test",
            self.gate if self.gated else None,
            now=lambda: 100.0,
            id_factory=lambda: next(ids),
        )
        self.engine.configure_publication(self.publication_context)

    def observe(self, *observations: Observation) -> None:
        self.state = replay(*observations, state=self.state)

    async def gate(self, binding: SentenceBinding) -> PublicationDecision:
        return state_publication_decision(
            binding, self.state, now=lambda: 100.0, clock_domain="test"
        )

    def speech(self, question: StateQuestion, _language: str | None) -> StateAnswer:
        fact = state_fact(question, self.state)
        return StateAnswer(
            render(fact.proposition, "en"), state_provenance(question, fact, self.state)
        )

    @property
    def is_speaking(self) -> bool:
        return self.engine.is_speaking

    async def speak(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> None:
        await self.engine.speak(
            utterance, sentence_index=sentence_index, transcript_id=transcript_id, language=language
        )

    async def prepare(
        self,
        utterance: str,
        *,
        sentence_index: int | None = None,
        transcript_id: str | None = None,
        language: str | None = None,
    ) -> PreparedSpeech | None:
        return await self.engine.prepare(
            utterance, sentence_index=sentence_index, transcript_id=transcript_id, language=language
        )

    def bind_publication(self, prepared: PreparedSpeech, binding: SentenceBinding) -> None:
        self.bindings.append(binding)
        self.engine.bind_publication(prepared, binding)

    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        if self.before_playback is not None:
            self.before_playback()
        await self.engine.speak_prepared(prepared)

    async def stop_playback(self) -> None:
        await self.engine.stop()

    async def stop(self) -> None:
        return None

    def raise_if_failed(self) -> None:
        return None

    async def wait_failed(self) -> None:
        await asyncio.Event().wait()

    async def ask(self, question: StateQuestion, transcript_id: str = "t1") -> None:
        planned = PlannedTurn(
            TurnPlan("p", "speech_only", output="speech", state_question=question)
        )
        runtime = TurnTestContext(
            speaker=self, journal=RecordingJournal(), state_speech=self.speech
        )
        final = TranscriptEvent(
            id=transcript_id,
            timestamp="2026-11-14T00:00:00+00:00",
            source="meeting_audio",
            text="Question?",
            finalized=True,
            participant_id="voice:a",
            speaker_evidence_id=None,
        )
        assert await turn_graph.answer_state(runtime, final, planned, 0) is True
        self.notes.extend(runtime.notes)

    @property
    def dropped(self) -> str:
        assert self.events[-1].kind == "playback_dropped"
        return self.events[-1].detail


async def test_presenter_answer_with_matching_revision_is_published_with_its_binding() -> None:
    meeting = Meeting()
    try:
        await meeting.ask("presenter")
        [binding] = meeting.bindings
        assert meeting.writer.written
        assert meeting.notes == ["Ayu is presenting."]
        assert binding.scope == "state"
        assert binding.publication_id == "publication-1"
        assert binding.request_id == "t1:0"
        assert binding.sentence_hash == sentence_digest("Ayu is presenting.")
        assert binding.evidence == ()
        state = binding.state
        assert state is not None
        assert (state.question, state.namespace, state.modality) == (
            "presenter",
            "meeting_live",
            "participant_claimed",
        )
        assert (state.domain, state.revision) == ("screen", AYU_SHARING.revisions.screen)
        assert state.subject_id == "dev-ayu"
        [started] = [e for e in meeting.events if e.kind == "playback_audio_started"]
        assert started.utterance.publication is binding
    finally:
        await meeting.engine.close()


async def test_screen_title_answer_carries_screen_state_provenance() -> None:
    meeting = Meeting()
    try:
        await meeting.ask("screen_title")
        [binding] = meeting.bindings
        assert meeting.writer.written
        state = binding.state
        assert state is not None
        assert (state.question, state.modality) == ("screen_title", "screen_read")
        screen = AYU_SHARING.world.screen
        assert screen is not None
        assert state.subject_id == f"dev-ayu@{screen.since_seq}"
    finally:
        await meeting.engine.close()


@pytest.mark.parametrize(
    ("question", "change", "reason"),
    [
        ("presenter", BUDI_TAKES_OVER, "state_subject_changed"),
        ("presenter", SHARING_STOPS, "state_subject_changed"),
        ("presenter", PROTOCOL_STALE, "state_source_unknown"),
        ("screen_title", ScreenObserved(title="Budget"), "state_subject_changed"),
        ("screen_title", SHARING_STOPS, "state_subject_changed"),
        ("screen_title", PROTOCOL_STALE, "state_source_unknown"),
    ],
)
async def test_state_change_while_queued_prevents_the_old_claim(
    question: StateQuestion, change: Observation, reason: str
) -> None:
    meeting = Meeting()
    meeting.before_playback = lambda: meeting.observe(change)
    try:
        await meeting.ask(question)
        assert len(meeting.bindings) == 1
        assert meeting.writer.written == []
        assert reason in meeting.dropped
        assert meeting.notes == []
    finally:
        await meeting.engine.close()


async def test_unrelated_screen_revision_keeps_an_unchanged_presenter_claim() -> None:
    meeting = Meeting()
    meeting.before_playback = lambda: meeting.observe(ScreenObserved(title="Budget"))
    try:
        await meeting.ask("presenter")
        [binding] = meeting.bindings
        assert binding.state is not None
        assert meeting.state.revisions.screen > binding.state.revision
        assert meeting.writer.written
    finally:
        await meeting.engine.close()


async def test_repeated_state_answers_get_distinct_publication_ids() -> None:
    meeting = Meeting()
    try:
        await meeting.ask("presenter", "t1")
        await meeting.ask("presenter", "t2")
        first, second = meeting.bindings
        assert first.sentence_hash == second.sentence_hash
        assert (first.publication_id, second.publication_id) == ("publication-1", "publication-2")
        started = [e for e in meeting.events if e.kind == "playback_audio_started"]
        assert [e.utterance.publication for e in started] == [first, second]
    finally:
        await meeting.engine.close()


@pytest.mark.parametrize("question", sorted(CURRENT_STATE_QUESTIONS))
@pytest.mark.parametrize("gated", [True, False])
async def test_unknown_state_abstention_still_speaks(
    question: StateQuestion, *, gated: bool
) -> None:
    meeting = Meeting(gated=gated)
    meeting.observe(PROTOCOL_STALE)
    try:
        await meeting.ask(question)
        [binding] = meeting.bindings
        assert binding.scope == "no_visual"
        assert binding.state is not None
        assert binding.state.known is False
        assert binding.state.subject_id is None
        assert binding.state.source_health == "stale"
        assert meeting.writer.written
        assert len(meeting.notes) == 1
    finally:
        await meeting.engine.close()


async def test_abstention_stays_speakable_after_the_source_recovers() -> None:
    meeting = Meeting()
    meeting.observe(PROTOCOL_STALE)
    meeting.before_playback = lambda: meeting.observe(
        SourceHealthObserved(source="meet_protocol", status="healthy")
    )
    try:
        await meeting.ask("presenter")
        assert meeting.writer.written
    finally:
        await meeting.engine.close()


async def test_factual_state_claim_without_a_gate_is_unresolved() -> None:
    meeting = Meeting(gated=False)
    try:
        await meeting.ask("presenter")
        assert meeting.writer.written == []
        assert "unresolved" in meeting.dropped
        assert meeting.notes == []
    finally:
        await meeting.engine.close()


async def test_nobody_presenting_is_a_factual_claim_that_is_revalidated() -> None:
    meeting = Meeting()
    meeting.observe(SHARING_STOPS)
    meeting.before_playback = lambda: meeting.observe(BUDI_TAKES_OVER)
    try:
        await meeting.ask("presenter")
        [binding] = meeting.bindings
        assert binding.scope == "state"
        assert meeting.writer.written == []
    finally:
        await meeting.engine.close()


def _state_plan(event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
    del reading, channel
    return PlannedTurn(
        TurnPlan(f"plan-{event.id}", "speech_only", output="speech", state_question="presenter")
    )


@pytest.mark.parametrize("prevented", [True, False])
async def test_actor_counts_a_state_answer_as_spoken_only_when_it_was_emitted(
    *, prevented: bool
) -> None:
    meeting = Meeting()
    if prevented:
        meeting.before_playback = lambda: meeting.observe(BUDI_TAKES_OVER)
    observed: list[str] = []
    actor = FloorActor(
        build_turn_graph(),
        ActorWiring(
            journal=FakeJournal(),
            responder=ModelResponder(
                chunks=unused_chunks, phase=ToolPhaseState(), max_sentences=3, language="en"
            ),
        ),
    )
    actor.observe_spoken(observed.append)
    actor.bind(meeting)
    actor.configure_deferred(DeferredOptions(planning=_state_plan, state_speech=meeting.speech))
    try:
        await actor.handle_final(transcript("f1", "Who is presenting?"))
        assert len(meeting.bindings) == 1
        assert bool(meeting.writer.written) is not prevented
        assert observed == ([] if prevented else ["Ayu is presenting."])
    finally:
        await actor.close()
        await meeting.engine.close()


def _state_binding(*, known: bool = True, scope: str = "state") -> SentenceBinding:
    fact = state_fact("presenter", AYU_SHARING)
    provenance = replace(state_provenance("presenter", fact, AYU_SHARING), known=known)
    return SentenceBinding(
        "pub",
        "meeting",
        "turn",
        "transcript",
        "request",
        0,
        "hash",
        None,
        (),
        (),
        scope,
        provenance,
    )


def test_state_scope_requires_known_state_provenance() -> None:
    with pytest.raises(ValueError, match="known fact"):
        _state_binding(known=False)
    with pytest.raises(ValueError, match="known fact"):
        SentenceBinding("pub", "m", "t", "t", "r", 0, "hash", None, (), (), "state")
    with pytest.raises(ValueError, match="abstention"):
        _state_binding(known=True, scope="no_visual")
    assert _state_binding(known=False, scope="no_visual").state is not None


def test_state_provenance_never_fabricates_visual_evidence() -> None:
    binding = _state_binding()
    assert binding.evidence == ()
    assert binding.required_kinds == ()
    assert binding.gather_attempt_id is None


async def test_visual_policy_does_not_approve_a_state_claim() -> None:
    async def never(_item: EvidenceBinding) -> Validation:
        raise AssertionError

    decision = await evaluate_visual_publication(
        _state_binding(), never, now=lambda: 100.0, clock_domain="test"
    )
    assert decision.validity == "unknown"


def test_regressed_or_foreign_state_source_is_unknown() -> None:
    binding = _state_binding()
    assert binding.state is not None
    earlier = replay(IN_CALL, RosterObserved(endpoints=(TWIN, AYU, AYU_SCREEN), synced=True))
    decision = state_publication_decision(binding, earlier, now=lambda: 100.0, clock_domain="test")
    assert (decision.validity, decision.reason) == ("unknown", "state_revision_regressed")
    foreign = replace(binding.state, question="attendance")
    assert latest_state_provenance(foreign, AYU_SHARING) is None


def test_participant_observation_of_a_state_claim_is_never_reported_compliant() -> None:
    binding = _state_binding()
    heard = AudioPublication(
        "pub",
        "transcript",
        0,
        "hash",
        100.0,
        101.0,
        "test",
        "participant_audible",
        0.01,
        participant_consent=True,
    )
    allowed = measure_publication(
        binding,
        heard,
        decision=PublicationDecision("valid", "ok", 99.9, "test"),
        clock_domain="test",
    )
    assert (allowed["status"], allowed["reason"]) == (
        "not_measurable",
        "state_continuity_unmeasurable",
    )
    assert binding.state is not None
    assert allowed["state_fact_id"] == binding.state.fact_id
    vetoed = measure_publication(
        binding,
        heard,
        decision=PublicationDecision("invalid", "state_changed", 99.9, "test"),
        clock_domain="test",
    )
    assert vetoed["status"] == "observed_noncompliant"


def test_state_provenance_type_is_separate_from_visual_snapshots() -> None:
    fact = state_fact("screen_title", AYU_SHARING)
    provenance = state_provenance("screen_title", fact, AYU_SHARING)
    assert isinstance(provenance, StateProvenance)
    assert provenance.fact_id == fact.fact_id
    assert provenance.source_health in (None, "healthy")
