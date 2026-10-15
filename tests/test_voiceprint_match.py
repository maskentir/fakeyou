from __future__ import annotations

import math
import struct
import threading
import time
from collections.abc import Sequence
from typing import override

import pytest

from fakeyou.db.voiceprint_store import VoiceprintRecord
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder
from fakeyou.identity.voiceprint_match import (
    CLIP_GRID_MS,
    CLIP_PAD_MS,
    VoiceprintMatcher,
    VoiceScoreAttribution,
    VoiceScores,
    apply_voice_scores,
    cut_turn_clip,
    score_turn_audio,
    score_turn_audio_async,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal
from tests._voiceprint_fakes import CONSENT_AT, FakeEmbedder, InMemoryVoiceprints, make_record


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
        owner_name=record.owner_name,
    )


def test_match_above_threshold_with_margin_verifies_winner() -> None:
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints([make_record("alice", (1.0, 0.0)), make_record("bob", (0.0, 1.0))])
    )

    scores = matcher.match((1.0, 0.0))

    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
    assert scores.s1 == 1.0
    assert scores.s2 == 0.0


def test_match_below_threshold_reports_unknown() -> None:
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints(
            [make_record("alice", (1.0, 0.0, 0.0)), make_record("bob", (0.0, 1.0, 0.0))]
        )
    )

    scores = matcher.match((0.0, 0.0, 1.0))

    assert scores.outcome == "unknown"
    assert scores.s1 == 0.0
    assert scores.s2 == 0.0


def test_match_close_runners_up_reports_conflict() -> None:
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints([make_record("alice", (1.0, 0.0)), make_record("bob", (0.99, 0.1))])
    )

    scores = matcher.match((1.0, 0.0))

    assert scores.outcome == "conflict"
    assert scores.person_id == "alice"
    assert scores.s1 == 1.0
    assert scores.s1 - scores.s2 < 0.15


def test_match_negative_winner_reports_negative() -> None:
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints(
            [make_record("alice", (1.0, 0.0)), make_negative("twin-voice", (0.0, 1.0))]
        )
    )

    scores = matcher.match((0.0, 1.0))

    assert scores.outcome == "negative"
    assert scores.person_id == "alice"


def test_match_empty_store_reports_unknown_with_floor_scores() -> None:
    matcher = VoiceprintMatcher(InMemoryVoiceprints())

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
        registry,
        scores,
        attribution=VoiceScoreAttribution(row.uuid, "dev-q", {}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
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
        VoiceScores("alice", 0.9, 0.1, "verified"),
        attribution=VoiceScoreAttribution(row.uuid, "dev-q", {"dev-q": "alice"}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
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
        VoiceScores("alice", 0.9, 0.1, "verified"),
        attribution=VoiceScoreAttribution(row.uuid, "dev-q", {"dev-q": "bob"}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
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
        VoiceScores("alice", 0.2, 0.1, "negative"),
        attribution=VoiceScoreAttribution(row.uuid, "dev-q", {}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
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
        VoiceScores("alice", 0.9, 0.85, "conflict"),
        attribution=VoiceScoreAttribution(row.uuid, "dev-q", {"dev-q": "bob"}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
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
        VoiceScores("alice", 0.9, 0.1, "verified"),
        attribution=VoiceScoreAttribution("0" * 32, None, {}),
        diagnostics=Diagnostics(journal=journal),
    )

    assert outcome == "verified"
    [decided] = journal.events("voiceprint_decided")
    assert decided["outcome"] == "verified"


def test_score_turn_audio_below_min_seconds_discards_without_scoring() -> None:
    embedder = FakeEmbedder((1.0, 0.0))
    matcher = VoiceprintMatcher(InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]))

    assert score_turn_audio(b"\x00" * 95998, embedder, matcher, min_seconds=3.0) is None

    assert embedder.calls == []


def test_score_turn_audio_at_min_seconds_embeds_and_matches() -> None:
    embedder = FakeEmbedder((1.0, 0.0))
    matcher = VoiceprintMatcher(InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]))

    scores = score_turn_audio(b"\x00" * 96000, embedder, matcher, min_seconds=3.0)

    assert scores is not None
    assert scores.outcome == "verified"
    assert scores.person_id == "alice"
    [utterances] = embedder.calls
    [utterance] = utterances
    assert len(utterance) == 48000


def test_score_turn_audio_scales_int16_samples_to_unit_floats() -> None:
    embedder = FakeEmbedder((1.0, 0.0))
    matcher = VoiceprintMatcher(InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]))
    clip = struct.pack("<4h", 0, 16384, -32768, 32767)

    scores = score_turn_audio(clip, embedder, matcher, min_seconds=0.0)

    assert scores is not None
    [utterances] = embedder.calls
    [utterance] = utterances
    assert list(utterance) == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768.0])


class _CountingVoiceprints(InMemoryVoiceprints):
    def __init__(self, records: list[VoiceprintRecord]) -> None:
        super().__init__(records)
        self.loads = 0

    @override
    def load_all(self) -> list[VoiceprintRecord]:
        self.loads += 1
        return super().load_all()


def test_matcher_loads_voiceprints_once_per_session() -> None:
    store = _CountingVoiceprints([make_record("alice", (1.0, 0.0))])
    matcher = VoiceprintMatcher(store)

    matcher.match((1.0, 0.0))
    store.save(make_record("bob", (0.0, 1.0)))
    second = matcher.match((1.0, 0.0))

    assert store.loads == 1
    assert second.person_id == "alice"


class _SleepingEmbedder(SpeakerEmbedder):
    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds
        self.threads: list[int] = []

    @override
    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        self.threads.append(threading.get_ident())
        time.sleep(self._delay)
        return (1.0, 0.0)


async def test_score_turn_audio_async_scores_off_the_event_loop() -> None:
    embedder = _SleepingEmbedder(0.01)
    matcher = VoiceprintMatcher(InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]))

    scores = await score_turn_audio_async(
        b"\x00" * 96000, embedder, matcher, min_seconds=3.0, timeout_seconds=5.0
    )

    assert scores is not None
    assert scores.outcome == "verified"
    assert embedder.threads != [threading.get_ident()]


class _GatedEmbedder(SpeakerEmbedder):
    def __init__(self) -> None:
        self.release = threading.Event()

    @override
    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        self.release.wait(5.0)
        return (1.0, 0.0)


async def test_score_turn_audio_async_times_out_slow_scoring() -> None:
    embedder = _GatedEmbedder()
    matcher = VoiceprintMatcher(InMemoryVoiceprints([make_record("alice", (1.0, 0.0))]))

    try:
        with pytest.raises(TimeoutError):
            await score_turn_audio_async(
                b"\x00" * 96000,
                embedder,
                matcher,
                min_seconds=3.0,
                timeout_seconds=0.05,
            )
    finally:
        embedder.release.set()


def _turn_audio(seconds: float) -> bytes:
    total = int(seconds * CAPTURE_SAMPLE_RATE)
    samples = [
        int(20000.0 * math.sin(index * 0.05) + 5000.0 * math.sin(index * 0.013))
        for index in range(total)
    ]
    return struct.pack("<" + "h" * total, *samples)


class ContentEmbedder(SpeakerEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    @override
    def embed(self, utterances: Sequence[Sequence[float]]) -> tuple[float, ...]:
        self.calls += 1
        flat = [sample for utterance in utterances for sample in utterance]
        mean = sum(flat) / len(flat)
        rms = math.sqrt(sum(sample * sample for sample in flat) / len(flat))
        norm = math.hypot(mean, rms) or 1.0
        return (mean / norm, rms / norm)


def test_clips_with_bounds_inside_one_grid_cell_are_byte_identical() -> None:
    audio = _turn_audio(6.0)

    first = cut_turn_clip(audio, 1050.0, 2310.0)
    second = cut_turn_clip(audio, 1090.0, 2350.0)

    assert first == second
    assert first == audio[800 * 32 : 2600 * 32]


def test_short_segment_gets_exact_padding_clamped_at_audio_start() -> None:
    assert CLIP_GRID_MS == 100
    assert CLIP_PAD_MS == 200
    audio = _turn_audio(6.0)

    clip = cut_turn_clip(audio, 50.0, 150.0)

    assert clip == audio[: 400 * 32]


def test_identical_turn_scores_zero_difference_across_workers() -> None:
    audio = _turn_audio(6.0)
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints([make_record("alice", (0.0, 1.0)), make_record("bob", (1.0, 0.0))])
    )

    worker_a = cut_turn_clip(audio, 1050.0, 2310.0)
    worker_b = cut_turn_clip(audio, 1090.0, 2350.0)

    assert worker_a == worker_b
    scores_a = score_turn_audio(worker_a, ContentEmbedder(), matcher, min_seconds=1.0)
    scores_b = score_turn_audio(worker_b, ContentEmbedder(), matcher, min_seconds=1.0)

    assert scores_a is not None
    assert scores_b is not None
    assert scores_a == scores_b
    assert scores_a.s1 - scores_b.s1 == 0


def test_match_threads_owner_name_and_consent_into_scores() -> None:
    matcher = VoiceprintMatcher(
        InMemoryVoiceprints(
            [
                make_record("spk-1", (1.0, 0.0), owner_name="Maya"),
                make_record("spk-2", (0.0, 1.0), owner_name="Rani"),
            ]
        )
    )

    scores = matcher.match((1.0, 0.0))

    assert scores.person_id == "spk-1"
    assert scores.owner_name == "Maya"
    assert scores.consent_ts == CONSENT_AT
