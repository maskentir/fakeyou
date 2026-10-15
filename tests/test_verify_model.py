from fakeyou.identity.verify import (
    VerificationInputs,
    claim_of,
    decide_verified,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._persistence_fakes import RecordingJournal
from tests._verify_support import (
    ARTIFACT,
    CONSENT,
    DIGEST_A,
    DIGEST_B,
    MODEL_VERSION,
    fake_embedder,
    misbind_kinds,
    named_registry,
    owner_scores,
    passing_scores,
    resolved_registry,
    stamped_artifact,
)


def test_decide_refuses_artifact_embedder_model_mismatch() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    decision = decide_verified(
        row.uuid,
        passing_scores(),
        VerificationInputs(
            artifact=stamped_artifact(f"model-a.onnx:{DIGEST_A}"),
            claim=claim_of(row),
            slot=row.slot,
            embedder=fake_embedder(f"model-b.onnx:{DIGEST_B}"),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )
    assert decision is None
    assert journal.events("name_verified") == []
    assert "voiceprint_model_mismatch" in misbind_kinds(faults)


def test_decide_accepts_matching_digest_with_different_model_name() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    decision = decide_verified(
        row.uuid,
        passing_scores(),
        VerificationInputs(
            artifact=stamped_artifact(f"container.onnx:{DIGEST_A}"),
            claim=claim_of(row),
            slot=row.slot,
            embedder=fake_embedder(f"host-other.onnx:{DIGEST_A}"),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )
    assert decision is not None


def test_decide_refuses_stamped_artifact_with_unparsable_embedder_id() -> None:
    registry = resolved_registry()
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    decision = decide_verified(
        row.uuid,
        passing_scores(),
        VerificationInputs(
            artifact=stamped_artifact(f"model-a.onnx:{DIGEST_A}"),
            claim=claim_of(row),
            slot=row.slot,
            embedder=fake_embedder("embed-test-v1"),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )
    assert decision is None
    assert journal.events("name_verified") == []


def test_journal_model_version_comes_from_embedder_model_id_and_stays_stable() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    assert fake_embedder().model_id == MODEL_VERSION
    scores = owner_scores("Maya", CONSENT)

    first = decide_verified(
        row.uuid,
        scores,
        VerificationInputs(
            artifact=ARTIFACT, claim="Maya", overlap=False, slot=row.slot, embedder=fake_embedder()
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )
    assert first is not None
    registry.reset()
    second = decide_verified(
        row.uuid,
        scores,
        VerificationInputs(
            artifact=ARTIFACT,
            claim="Maya",
            overlap=False,
            slot=row.slot,
            embedder=fake_embedder("eres2net:deadbeef"),
        ),
        registry,
        Diagnostics(faults=faults, journal=journal),
    )

    assert second is not None
    events = journal.events("name_verified")
    assert [event["model_version"] for event in events] == [MODEL_VERSION, "eres2net:deadbeef"]


def test_embedder_without_model_id_returns_null() -> None:
    registry = named_registry("Maya")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    row = registry.resolve(0)
    assert not isinstance(row, str)
    scores = owner_scores("Maya", CONSENT)

    assert (
        decide_verified(
            row.uuid,
            scores,
            VerificationInputs(
                artifact=ARTIFACT,
                claim="Maya",
                overlap=False,
                slot=row.slot,
                embedder=fake_embedder(None),
            ),
            registry,
            Diagnostics(faults=faults, journal=journal),
        )
        is None
    )
    assert (
        decide_verified(
            row.uuid,
            scores,
            VerificationInputs(
                artifact=ARTIFACT, claim="Maya", overlap=False, slot=row.slot, embedder=None
            ),
            registry,
            Diagnostics(faults=faults, journal=journal),
        )
        is None
    )
    assert faults.report() is None
    assert journal.events("name_verified") == []
