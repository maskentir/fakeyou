import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, override

import pytest

from fakeyou.audio.playback import PlaybackEvent
from fakeyou.conversation.actor import ActorWiring, DeferredOptions, FloorActor, ReplyOptions
from fakeyou.conversation.model_responder import ReplierWholeResponder
from fakeyou.conversation.turn_graph import PlannedTurn, SentenceLimits, build_turn_graph
from fakeyou.foundation.gathered_evidence import GatheredEvidence
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationContext,
    PublicationDecision,
    SentenceBinding,
    SnapshotRef,
    Validation,
    evaluate_visual_publication,
)
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.orchestration.spoken_log import SpokenLog
from fakeyou.persistence.record import MeetingRecord
from fakeyou.session.intent import Reading
from fakeyou.session.planner import TurnPlan
from fakeyou.session.policy import Channel
from fakeyou.speech.contract import PlaybackReceipt, PreparedSpeech, ReceiptObserver
from tests._audio_support import FakeSynth, FakeWriter, build_engine
from tests._model_responder import ModelResponder
from tests._persistence_fakes import RecordingJournal
from tests.test_turn_graph_whole_adapter import (
    FakeJournal,
    FakeReplier,
    FakeSession,
    ToolPhaseState,
    transcript,
    unused_chunks,
)


class BindingSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[PlaybackEvent] = []
        self.writer = FakeWriter()
        self.engine = build_engine(FakeSynth(), self.writer, self.events)
        ids = iter(("publication-1", "publication-2"))
        self.publication_context = PublicationContext(
            meeting_id="meeting-1",
            clock_domain="test",
            id_factory=lambda: next(ids),
            now=lambda: 100.0,
            gate=self.gate,
        )
        self.engine.configure_publication(self.publication_context)
        self.bindings: list[tuple[PreparedSpeech, SentenceBinding]] = []
        self.scene_changed = False

    def observe_receipts(self, observer: ReceiptObserver) -> None:
        self.engine.observe_receipts(observer)

    async def gate(self, binding: SentenceBinding) -> PublicationDecision:
        if self.scene_changed:
            return PublicationDecision("invalid", "scene_changed", 100.0, "test")

        async def validate(item: EvidenceBinding) -> Validation:
            return Validation(
                100.0,
                "test",
                item.snapshot,
                supported=True,
                unchanged=True,
                observed_at=100.0,
                uncertainty_seconds=0.0,
            )

        return await evaluate_visual_publication(
            binding, validate, now=lambda: 100.0, clock_domain="test"
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
        self.bindings.append((prepared, binding))
        self.engine.bind_publication(prepared, binding)

    @override
    async def speak_prepared(self, prepared: PreparedSpeech) -> None:
        await self.engine.speak_prepared(prepared)
        self.spoken.append(prepared.text)


def provenance(kind: str) -> EvidenceBinding:
    return EvidenceBinding(
        kind=kind,
        gather_attempt_id="gather-1",
        snapshot=SnapshotRef(
            "screen" if kind == "screen" else "camera",
            "screen#1",
            "stream-1",
            1,
            1,
            1,
            datetime(2026, 11, 14, tzinfo=UTC),
        ),
        observed_at=99.0,
        provider="fake",
        provider_attempt_id="provider-1",
        inference="inference",
        capture_monotonic=98.0,
        capture_clock_domain="utc",
        capture_mapping_clock_domain="test",
        capture_time_origin="local_acquisition",
        capture_mapping_uncertainty_seconds=0.0,
        observation_clock_domain="test",
    )


@pytest.mark.parametrize("kind", ["screen", "participant"])
async def test_publication_binding_is_completed_after_sentence_scrubbing(
    kind: Literal["screen", "participant"],
    tmp_path: Path,
) -> None:
    gathered = GatheredEvidence(
        {kind: {"code": "ok", "answer": "A red mug."}}, "gather-1", (provenance(kind),)
    )

    async def evidence(
        _question: str, _needs: tuple[str, ...], _transcript_id: str | None = None
    ) -> Mapping[str, object]:
        return gathered

    def planning(event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
        del reading, channel
        return PlannedTurn(TurnPlan(f"plan-{event.id}", "evidence_then_speech", "speech", (kind,)))

    original = "The mug is red [screen#1]."
    replier = FakeReplier(reply=original)
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
    session = BindingSession()
    actor.bind(session)
    actor.configure_deferred(DeferredOptions(planning=planning, evidence=evidence))
    try:
        await actor.handle_final(transcript("f1", "What is on the screen?"))
        [prepared, binding] = session.bindings[0]
        assert len(session.bindings) == 1
        assert prepared.text != original
        assert (
            binding.sentence_hash
            == hashlib.sha256(" ".join(prepared.text.split()).encode()).hexdigest()
        )
        assert binding.sentence_hash != hashlib.sha256(original.encode()).hexdigest()
        assert binding.meeting_id == "meeting-1"
        assert binding.turn_id == binding.transcript_id == "f1"
        assert binding.request_id == "f1:1"
        assert binding.publication_id == "publication-1"
        assert binding.sentence_index == prepared.sentence_index == 0
        assert binding.gather_attempt_id == "gather-1"
        assert binding.evidence == gathered.provenance
        assert session.spoken == [prepared.text]
        assert session.writer.written
        [started] = [e for e in session.events if e.kind == "playback_audio_started"]
        assert started.utterance.publication is binding
        journal = RecordingJournal()
        record = MeetingRecord(tmp_path / "record.jsonl")
        try:
            SpokenLog(journal, record).observe(started)
            [row] = journal.events("playback_audio_started")
            assert row["sentence_sha256"] == binding.sentence_hash
            assert row["publication_id"] == binding.publication_id
            assert row["request_id"] == binding.request_id
            assert row["publication_meeting_id"] == binding.meeting_id
            assert "audible_start" not in row
        finally:
            record.close()
    finally:
        await actor.close()
        await session.engine.close()


@pytest.mark.parametrize("scene_changed", [True, False])
async def test_visual_reply_is_counted_as_spoken_only_when_pcm_was_written(
    *, scene_changed: bool
) -> None:
    gathered = GatheredEvidence(
        {"screen": {"code": "ok", "answer": "A red mug."}}, "gather-1", (provenance("screen"),)
    )

    async def evidence(
        _question: str, _needs: tuple[str, ...], _transcript_id: str | None = None
    ) -> Mapping[str, object]:
        return gathered

    def planning(event: TranscriptEvent, reading: Reading, channel: Channel) -> PlannedTurn:
        del reading, channel
        return PlannedTurn(
            TurnPlan(f"plan-{event.id}", "evidence_then_speech", "speech", ("screen",))
        )

    replier = FakeReplier(reply="The mug is red.")
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
    session = BindingSession()
    session.scene_changed = scene_changed
    counted: list[PlaybackReceipt] = []
    actor.observe_spoken(counted.append)
    actor.bind(session)
    actor.configure_deferred(DeferredOptions(planning=planning, evidence=evidence))
    try:
        await actor.handle_final(transcript("f1", "What is on the screen?"))
        [(prepared, binding)] = session.bindings
        assert bool(session.writer.written) is not scene_changed
        assert [item.text for item in counted] == ([] if scene_changed else [prepared.text])
        assert all(item.publication_id == binding.publication_id for item in counted)
    finally:
        await actor.close()
        await session.engine.close()
