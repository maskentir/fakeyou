import json
import statistics
from pathlib import Path

from fakeyou.faults import FaultReporter
from fakeyou.identity import SpeakerRegistry
from fakeyou.verify import (
    CalibrationArtifact,
    VerifiedDecision,
    claim_of,
    decide_verified,
    load_calibration_artifact,
)
from fakeyou.voiceprint_embed import SpeakerEmbedder
from fakeyou.voiceprint_match import VoiceprintMatcher, VoiceScores, apply_voice_scores
from fakeyou.voiceprint_store import VoiceprintRecord, VoiceprintStore
from tests.fakes import RecordingJournal

ARTIFACT = CalibrationArtifact(
    theta_llr=2.0,
    margin_floor=0.15,
    snorm_cohort=(0.1, 0.2, 0.3, 0.4),
    platt_a=1.0,
    platt_b=0.0,
    platt_version="platt-v1",
    report_id="cal-001",
)

MODEL_VERSION = "embed-test-v1"

CONSENT = "2026-10-07T10:00:00+00:00"


class FakeStore(VoiceprintStore):
    def __init__(self, records: list[VoiceprintRecord]) -> None:
        self._records = list(records)

    def load_all(self) -> list[VoiceprintRecord]:
        return list(self._records)


def _embedder(model_id: str | None = MODEL_VERSION) -> SpeakerEmbedder:
    return SpeakerEmbedder(object(), model_id=model_id)


def _named_registry(name: str) -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": name}])
    return registry


def _stored_record(
    person_id: str,
    owner_name: str,
    embedding: tuple[float, ...],
    consent_ts: str = CONSENT,
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=consent_ts,
        policy_version="v1",
        is_negative=False,
        enrolled_at="2026-10-07T10:00:01+00:00",
        last_used_at="2026-10-07T10:00:02+00:00",
        owner_name=owner_name,
    )


def _owner_scores(owner_name: str | None, consent_ts: str | None) -> VoiceScores:
    return VoiceScores("spk-1", 0.9, 0.1, "verified", owner_name=owner_name, consent_ts=consent_ts)


def _registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": "alice"}])
    return registry


def _decide(
    registry: SpeakerRegistry,
    journal: RecordingJournal,
    faults: FaultReporter,
    scores: VoiceScores,
    claim: str | None,
    overlap: bool | None = None,
) -> VerifiedDecision | None:
    row = registry.resolve(0)
    assert not isinstance(row, str)
    return decide_verified(
        row.uuid,
        scores,
        claim,
        ARTIFACT,
        registry=registry,
        slot=row.slot,
        overlap=overlap,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )


def _misbind_kinds(faults: FaultReporter) -> set[str]:
    report = faults.report()
    assert report is not None
    return {entry["kind"] for entry in report["faults"]["kinds"]}


def test_passing_score_with_agreeing_claim_verifies_with_complete_journal() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert claim_of(row) == "alice"

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        claim_of(row),
    )

    assert decision is not None
    assert decision.uuid == row.uuid
    assert decision.person_id == "alice"
    assert decision.name == "alice"
    assert decision.margin == 0.8
    mean = statistics.mean(ARTIFACT.snorm_cohort)
    std = statistics.pstdev(ARTIFACT.snorm_cohort)
    assert decision.llr == (0.9 - mean) / std
    assert decision.llr >= ARTIFACT.theta_llr
    [event] = journal.events("name_verified")
    assert event["enrollment_id"] == "alice"
    assert event["model_version"] == MODEL_VERSION
    assert event["score_raw"] == 0.9
    assert event["snorm_mean"] == round(mean, 4)
    assert event["snorm_std"] == round(std, 4)
    assert event["snorm_n"] == 4
    assert event["platt_version"] == "platt-v1"
    assert event["theta_llr"] == 2.0
    assert event["report_id"] == "cal-001"
    assert event["claim_agree"] is True
    assert event["uuid"] == row.uuid
    assert event["slot"] == 0
    assert event["llr"] == round(decision.llr, 4)
    assert event["margin"] == 0.8
    assert event["source"] == "audio"
    assert faults.report() is None
    bound = registry.resolve(0)
    assert not isinstance(bound, str)
    assert bound.verified_name == "alice"
    assert bound.verified_enrollment == "alice"


def test_missing_artifact_returns_null() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    decision = decide_verified(
        row.uuid,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
        None,
        registry=registry,
        slot=row.slot,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )

    assert decision is None
    assert journal.events("name_verified") == []
    assert faults.report() is None


def test_mismatched_claim_returns_null_with_misbind() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "bob",
    )

    assert decision is None
    assert "voiceprint_misbind" in _misbind_kinds(faults)
    assert journal.events("name_verified") == []
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.verified_name is None
    assert row.verified_enrollment is None


def test_margin_below_floor_returns_null_without_fault() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.85, "conflict", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_explicit_overlap_returns_null_without_fault() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
        overlap=True,
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_multi_csrc_without_flag_returns_null_without_fault() -> None:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11, 22])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": "alice"}])
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_second_decision_for_other_person_conflicts_and_keeps_first() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    first = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )
    assert first is not None
    second = _decide(
        registry,
        journal,
        faults,
        VoiceScores("bob", 0.9, 0.1, "verified", owner_name="bob", consent_ts=CONSENT),
        "bob",
    )

    assert second is None
    assert "voiceprint_misbind" in _misbind_kinds(faults)
    assert len(journal.events("name_verified")) == 1
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.verified_name == "alice"
    assert row.verified_enrollment == "alice"


def test_reset_clears_verified_binding_for_fail_closed_session() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    first = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )
    assert first is not None
    registry.reset()
    rebound = decide_verified(
        row.uuid,
        VoiceScores("bob", 0.9, 0.1, "verified", owner_name="bob", consent_ts=CONSENT),
        "bob",
        ARTIFACT,
        registry=registry,
        slot=0,
        overlap=False,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )

    assert rebound is not None
    assert rebound.person_id == "bob"
    assert len(journal.events("name_verified")) == 2


def test_negative_match_never_verifies() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = _decide(
        registry,
        journal,
        faults,
        VoiceScores("alice", 0.9, 0.1, "negative", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_loader_reads_versioned_artifact_from_file(tmp_path: Path) -> None:
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2, 0.3, 0.4],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )

    assert load_calibration_artifact(path) == ARTIFACT


def test_loader_missing_file_returns_null(tmp_path: Path) -> None:
    assert load_calibration_artifact(tmp_path / "absent.json") is None


def test_loader_malformed_file_returns_null(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert load_calibration_artifact(broken) is None
    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"theta_llr": 2.0}))
    assert load_calibration_artifact(incomplete) is None
    typed = tmp_path / "typed.json"
    typed.write_text(
        json.dumps(
            {
                "theta_llr": "high",
                "margin_floor": 0.15,
                "snorm_cohort": [0.1, 0.2],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )
    assert load_calibration_artifact(typed) is None
    empty = tmp_path / "empty.json"
    empty.write_text(
        json.dumps(
            {
                "theta_llr": 2.0,
                "margin_floor": 0.15,
                "snorm_cohort": [],
                "platt_a": 1.0,
                "platt_b": 0.0,
                "platt_version": "platt-v1",
                "report_id": "cal-001",
            }
        )
    )
    assert load_calibration_artifact(empty) is None


def test_apply_voice_scores_forwards_to_verified_path() -> None:
    registry = _registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        device="dev-a",
        owners={},
        journal=journal,
        faults=faults,
        artifact=ARTIFACT,
        claim="alice",
        slot=row.slot,
        embedder=_embedder(),
    )

    assert outcome == "verified"
    [event] = journal.events("name_verified")
    assert event["enrollment_id"] == "alice"
    assert event["claim_agree"] is True
    rebound = registry.resolve(0)
    assert not isinstance(rebound, str)
    assert rebound.verified_name == "alice"


def test_claim_matching_owner_name_verifies_with_distinct_person_id() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    store = FakeStore(
        [
            _stored_record("spk-1", "Maya", (1.0, 0.0)),
            _stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    assert scores.person_id == "spk-1"
    assert scores.owner_name == "Maya"
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert claim_of(row) == "Maya"

    decision = decide_verified(
        row.uuid,
        scores,
        claim_of(row),
        ARTIFACT,
        registry=registry,
        slot=row.slot,
        overlap=False,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )

    assert decision is not None
    assert decision.person_id == "spk-1"
    assert decision.name == "Maya"
    [event] = journal.events("name_verified")
    assert event["enrollment_id"] == "spk-1"
    assert event["claim_agree"] is True
    assert event["model_version"] == MODEL_VERSION
    assert faults.report() is None
    bound = registry.resolve(0)
    assert not isinstance(bound, str)
    assert bound.verified_name == "Maya"
    assert bound.verified_enrollment == "spk-1"


def test_claim_matching_person_id_but_not_owner_returns_null_with_misbind() -> None:
    registry = _named_registry("spk-1")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    store = FakeStore(
        [
            _stored_record("spk-1", "Maya", (1.0, 0.0)),
            _stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert claim_of(row) == "spk-1"

    decision = decide_verified(
        row.uuid,
        scores,
        claim_of(row),
        ARTIFACT,
        registry=registry,
        slot=row.slot,
        overlap=False,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )

    assert decision is None
    assert "voiceprint_misbind" in _misbind_kinds(faults)
    assert journal.events("name_verified") == []


def test_missing_owner_name_returns_null_without_fault() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    store = FakeStore(
        [
            _stored_record("spk-1", "", (1.0, 0.0)),
            _stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    assert scores.owner_name == ""

    assert (
        decide_verified(
            row.uuid,
            scores,
            claim_of(row),
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=_embedder(),
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert (
        decide_verified(
            row.uuid,
            _owner_scores(None, CONSENT),
            claim_of(row),
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=_embedder(),
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_missing_consent_returns_null_without_fault() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    store = FakeStore(
        [
            _stored_record("spk-1", "Maya", (1.0, 0.0), consent_ts=""),
            _stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    assert scores.consent_ts == ""

    assert (
        decide_verified(
            row.uuid,
            scores,
            claim_of(row),
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=_embedder(),
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert (
        decide_verified(
            row.uuid,
            _owner_scores("Maya", None),
            claim_of(row),
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=_embedder(),
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_journal_model_version_comes_from_embedder_model_id_and_stays_stable() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert _embedder().model_id == MODEL_VERSION
    scores = _owner_scores("Maya", CONSENT)

    first = decide_verified(
        row.uuid,
        scores,
        "Maya",
        ARTIFACT,
        registry=registry,
        slot=row.slot,
        overlap=False,
        embedder=_embedder(),
        journal=journal,
        faults=faults,
    )
    assert first is not None
    registry.reset()
    second = decide_verified(
        row.uuid,
        scores,
        "Maya",
        ARTIFACT,
        registry=registry,
        slot=row.slot,
        overlap=False,
        embedder=_embedder("campplus:deadbeef"),
        journal=journal,
        faults=faults,
    )

    assert second is not None
    events = journal.events("name_verified")
    assert [event["model_version"] for event in events] == [MODEL_VERSION, "campplus:deadbeef"]


def test_embedder_without_model_id_returns_null() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    scores = _owner_scores("Maya", CONSENT)

    assert (
        decide_verified(
            row.uuid,
            scores,
            "Maya",
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=_embedder(None),
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert (
        decide_verified(
            row.uuid,
            scores,
            "Maya",
            ARTIFACT,
            registry=registry,
            slot=row.slot,
            overlap=False,
            embedder=None,
            journal=journal,
            faults=faults,
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_apply_voice_scores_without_embedder_skips_verified_path() -> None:
    registry = _named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    outcome = apply_voice_scores(
        registry,
        row.uuid,
        _owner_scores("Maya", CONSENT),
        device="dev-a",
        owners={},
        journal=journal,
        faults=faults,
        artifact=ARTIFACT,
        claim="Maya",
        slot=row.slot,
    )

    assert outcome == "verified"
    assert journal.events("name_verified") == []
