from fakeyou.identity.verify import (
    VerificationInputs,
    claim_of,
    decide_verified,
)
from fakeyou.identity.voiceprint_match import (
    VoiceprintMatcher,
    VoiceScoreAttribution,
    VoiceScores,
    apply_voice_scores,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal
from tests._verify_support import (
    ARTIFACT,
    CONSENT,
    MODEL_VERSION,
    fake_embedder,
    misbind_kinds,
    named_registry,
    owner_scores,
    resolved_registry,
    stored_record,
)
from tests._voiceprint_fakes import InMemoryVoiceprints


def test_apply_voice_scores_forwards_to_verified_path() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    outcome = apply_voice_scores(
        registry,
        VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT),
        attribution=VoiceScoreAttribution(row.uuid, "dev-a", {}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
        verification=VerificationInputs(
            artifact=ARTIFACT,
            claim="alice",
            slot=row.slot,
            embedder=fake_embedder(),
        ),
    )

    assert outcome == "verified"
    [event] = journal.events("name_verified")
    assert event["enrollment_id"] == "alice"
    assert event["claim_agree"] is True
    rebound = registry.resolve(0)
    assert not isinstance(rebound, str)
    assert rebound.verified_name == "alice"


def test_claim_matching_owner_name_verifies_with_distinct_person_id() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    store = InMemoryVoiceprints(
        [
            stored_record("spk-1", "Maya", (1.0, 0.0)),
            stored_record("spk-2", "Rani", (0.0, 1.0)),
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
        VerificationInputs(
            artifact=ARTIFACT,
            claim=claim_of(row),
            overlap=False,
            slot=row.slot,
            embedder=fake_embedder(),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
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
    registry = named_registry("spk-1")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    store = InMemoryVoiceprints(
        [
            stored_record("spk-1", "Maya", (1.0, 0.0)),
            stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert claim_of(row) == "spk-1"

    decision = decide_verified(
        row.uuid,
        scores,
        VerificationInputs(
            artifact=ARTIFACT,
            claim=claim_of(row),
            overlap=False,
            slot=row.slot,
            embedder=fake_embedder(),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )

    assert decision is None
    assert "voiceprint_misbind" in misbind_kinds(faults)
    assert journal.events("name_verified") == []


def test_missing_owner_name_returns_null_without_fault() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    store = InMemoryVoiceprints(
        [
            stored_record("spk-1", "", (1.0, 0.0)),
            stored_record("spk-2", "Rani", (0.0, 1.0)),
        ]
    )
    scores = VoiceprintMatcher(store).match((1.0, 0.0))
    assert scores.owner_name == ""

    assert (
        decide_verified(
            row.uuid,
            scores,
            VerificationInputs(
                artifact=ARTIFACT,
                claim=claim_of(row),
                overlap=False,
                slot=row.slot,
                embedder=fake_embedder(),
            ),
            registry,
            Diagnostics(faults=faults, journal=journal),
        )
        is None
    )
    assert (
        decide_verified(
            row.uuid,
            owner_scores(None, CONSENT),
            VerificationInputs(
                artifact=ARTIFACT,
                claim=claim_of(row),
                overlap=False,
                slot=row.slot,
                embedder=fake_embedder(),
            ),
            registry,
            Diagnostics(faults=faults, journal=journal),
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_missing_consent_returns_null_without_fault() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    assert (
        decide_verified(
            row.uuid,
            owner_scores("Maya", None),
            VerificationInputs(
                artifact=ARTIFACT,
                claim=claim_of(row),
                overlap=False,
                slot=row.slot,
                embedder=fake_embedder(),
            ),
            registry,
            Diagnostics(faults=faults, journal=journal),
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []


def test_apply_voice_scores_without_embedder_skips_verified_path() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)

    outcome = apply_voice_scores(
        registry,
        owner_scores("Maya", CONSENT),
        attribution=VoiceScoreAttribution(row.uuid, "dev-a", {}),
        diagnostics=Diagnostics(journal=journal, faults=faults),
        verification=VerificationInputs(
            artifact=ARTIFACT,
            claim="Maya",
            slot=row.slot,
        ),
    )

    assert outcome == "verified"
    assert journal.events("name_verified") == []
