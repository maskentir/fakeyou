from __future__ import annotations

from collections.abc import Sequence

from fakeyou.faults import FaultReporter
from fakeyou.identity import SpeakerRegistry
from fakeyou.voiceprint_embed import SpeakerEmbedder
from fakeyou.voiceprint_match import (
    VoiceprintMatcher,
    VoiceScores,
    apply_voice_scores,
    score_turn_audio,
)
from fakeyou.voiceprint_store import VoiceprintRecord, VoiceprintStore
from tests.fakes import RecordingJournal


class FakeStore(VoiceprintStore):
    def __init__(self, records: Sequence[VoiceprintRecord] = ()) -> None:
        self._records = list(records)

    def load_all(self) -> list[VoiceprintRecord]:
        return list(self._records)


class FakeEmbedder(SpeakerEmbedder):
    def __init__(self, vector: tuple[float, ...]) -> None:
        self._vector = vector
        self.calls: list[list[tuple[float, ...]]] = []

    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        self.calls.append([tuple(utterance) for utterance in utterances])
        return self._vector


def make_record(person_id: str, embedding: tuple[float, ...]) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts="2026-10-07T10:00:00+00:00",
        policy_version="v1",
        is_negative=False,
        enrolled_at="2026-10-07T10:00:01+00:00",
        last_used_at="2026-10-07T10:00:02+00:00",
    )


def make_negative(person_id: str, embedding: tuple[float, ...]) -> VoiceprintRecord:
    record = make_record(person_id, embedding)
    return VoiceprintRecord(
        person_id=record.person_id,
        embedding=record.embedding,
        dim=record.dim,
        n_utterances=record.n_utterances,
        consent_ts=record.consent_ts,
        policy_version=record.policy_version,
        is_negative=True,
        enrolled_at=record.enrolled_at,
        last_used_at=record.last_used_at,
    )


def test_match_above_threshold_with_margin_verifies_winner() -> None:
    matcher = VoiceprintMatcher(
        FakeStore([make_record("alice", (1.0, 0.0)), make_record("bob", (0.0, 1.0))])
    )

    scores = matcher.match((1.0, 0.0))

    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
    assert scores.s1 == 1.0
    assert scores.s2 == 0.0


def test_match_below_threshold_reports_unknown() -> None:
    matcher = VoiceprintMatcher(
        FakeStore([make_record("alice", (1.0, 0.0, 0.0)), make_record("bob", (0.0, 1.0, 0.0))])
    )

    scores = matcher.match((0.0, 0.0, 1.0))

    assert scores.outcome == "unknown"
    assert scores.s1 == 0.0
    assert scores.s2 == 0.0


def test_match_close_runners_up_reports_conflict() -> None:
    matcher = VoiceprintMatcher(
        FakeStore([make_record("alice", (1.0, 0.0)), make_record("bob", (0.99, 0.1))])
    )

    scores = matcher.match((1.0, 0.0))

    assert scores.outcome == "conflict"
    assert scores.person_id == "alice"
    assert scores.s1 == 1.0
    assert scores.s1 - scores.s2 < 0.15


def test_match_negative_winner_reports_negative() -> None:
    matcher = VoiceprintMatcher(
        FakeStore([make_record("alice", (1.0, 0.0)), make_negative("twin-voice", (0.0, 1.0))])
    )

    scores = matcher.match((0.0, 1.0))

    assert scores.outcome == "negative"
    assert scores.person_id == "alice"


def test_match_empty_store_reports_unknown_with_floor_scores() -> None:
    matcher = VoiceprintMatcher(FakeStore([]))

    scores = matcher.match((1.0, 0.0))

    assert scores == VoiceScores(None, -1.0, -1.0, "unknown")


def test_apply_verified_notes_scores_and_journals_verified() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    scores = VoiceScores("alice", 0.9, 0.1, "verified")

    outcome = apply_voice_scores(
        registry, row.uuid, scores, device="dev-q", owners={}, journal=journal, faults=faults
    )

    assert outcome == "verified"
    updated = registry.resolve(0)
    assert not isinstance(updated, str)
    assert updated.margin == 0.8
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "verified"
    assert decided["person_id"] == "alice"
    assert decided["score"] == 0.9
    assert decided["margin"] == 0.8
    assert decided["speaker_uuid"] == row.uuid
    assert faults.report() is None


def test_apply_matching_owner_verifies_without_fault() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        VoiceScores("alice", 0.9, 0.1, "verified"),
        device="dev-q",
        owners={"dev-q": "alice"},
        journal=journal,
        faults=faults,
    )

    assert outcome == "verified"
    assert faults.report() is None
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "verified"


def test_apply_verified_on_foreign_device_faults_and_keeps_binding() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.device == "dev-q"
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        VoiceScores("alice", 0.9, 0.1, "verified"),
        device="dev-q",
        owners={"dev-q": "bob"},
        journal=journal,
        faults=faults,
    )

    assert outcome == "mismatched_owner"
    report = faults.report()
    assert report is not None
    kinds = {entry["kind"] for entry in report["faults"]["kinds"]}
    assert "voiceprint_misbind" in kinds
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "mismatched_owner"
    assert decided["person_id"] == "alice"
    rebound = registry.resolve(0)
    assert not isinstance(rebound, str)
    assert rebound.device == "dev-q"
    assert rebound.margin == 0.8


def test_apply_negative_consumes_as_unknown_without_fault() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        VoiceScores("alice", 0.2, 0.1, "negative"),
        device="dev-q",
        owners={},
        journal=journal,
        faults=faults,
    )

    assert outcome == "unknown"
    assert faults.report() is None
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "unknown"


def test_apply_conflict_journals_conflict_without_fault() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    row = registry.resolve(0)
    assert not isinstance(row, str)
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        VoiceScores("alice", 0.9, 0.85, "conflict"),
        device="dev-q",
        owners={"dev-q": "bob"},
        journal=journal,
        faults=faults,
    )

    assert outcome == "conflict"
    assert faults.report() is None
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "conflict"


def test_apply_unknown_speaker_uuid_notes_nothing_but_journals() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-q"])
    registry.observe_stream(0, [11])
    journal = RecordingJournal()

    outcome = apply_voice_scores(
        registry,
        "0" * 32,
        VoiceScores("alice", 0.9, 0.1, "verified"),
        device=None,
        owners={},
        journal=journal,
    )

    assert outcome == "verified"
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "verified"


def test_score_turn_audio_below_min_seconds_discards_without_scoring() -> None:
    embedder = FakeEmbedder((1.0, 0.0))
    matcher = VoiceprintMatcher(FakeStore([make_record("alice", (1.0, 0.0))]))

    assert score_turn_audio(b"\x00" * 95998, embedder, matcher, min_seconds=3.0) is None

    assert embedder.calls == []


def test_score_turn_audio_at_min_seconds_embeds_and_matches() -> None:
    embedder = FakeEmbedder((1.0, 0.0))
    matcher = VoiceprintMatcher(FakeStore([make_record("alice", (1.0, 0.0))]))

    scores = score_turn_audio(b"\x00" * 96000, embedder, matcher, min_seconds=3.0)

    assert scores is not None
    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
    [utterances] = embedder.calls
    [utterance] = utterances
    assert len(utterance) == 48000
