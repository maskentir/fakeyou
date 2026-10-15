import statistics

from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import (
    VerificationInputs,
    claim_of,
    decide_verified,
)
from fakeyou.identity.voiceprint_match import VoiceScores
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal
from tests._verify_support import (
    ARTIFACT,
    CONSENT,
    MODEL_VERSION,
    decide_claim,
    fake_embedder,
    misbind_kinds,
    resolved_registry,
)


def test_passing_score_with_agreeing_claim_verifies_with_complete_journal() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert claim_of(row) == "alice"

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
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
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    decision = decide_verified(
        row.uuid,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        VerificationInputs(artifact=None, claim="alice", slot=row.slot, embedder=fake_embedder()),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )

    assert decision is None
    assert journal.events("name_verified") == []
    assert faults.report() is None


def test_mismatched_claim_returns_null_with_misbind() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "bob",
    )

    assert decision is None
    assert "voiceprint_misbind" in misbind_kinds(faults)
    assert journal.events("name_verified") == []
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.verified_name is None
    assert row.verified_enrollment is None


def test_margin_below_floor_returns_null_without_fault() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.85, "conflict", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_explicit_overlap_returns_null_without_fault() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
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

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_second_decision_for_other_person_conflicts_and_keeps_first() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    first = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )
    assert first is not None
    second = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("bob", 0.9, 0.1, "verified", owner_name="bob", consent_ts=CONSENT),
        "bob",
    )

    assert second is None
    assert "voiceprint_misbind" in misbind_kinds(faults)
    assert len(journal.events("name_verified")) == 1
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert row.verified_name == "alice"
    assert row.verified_enrollment == "alice"


def test_reset_clears_verified_binding_for_fail_closed_session() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    first = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )
    assert first is not None
    registry.reset()
    rebound = decide_verified(
        row.uuid,
        VoiceScores("bob", 0.9, 0.1, "verified", owner_name="bob", consent_ts=CONSENT),
        VerificationInputs(
            artifact=ARTIFACT, claim="bob", overlap=False, slot=0, embedder=fake_embedder()
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )

    assert rebound is not None
    assert rebound.person_id == "bob"
    assert len(journal.events("name_verified")) == 2


def test_negative_match_never_verifies() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    decision = decide_claim(
        registry,
        Diagnostics(faults=faults, journal=journal),
        VoiceScores("alice", 0.9, 0.1, "negative", owner_name="alice", consent_ts=CONSENT),
        "alice",
    )

    assert decision is None
    assert faults.report() is None
    assert journal.events("name_verified") == []
