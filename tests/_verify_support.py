from datetime import UTC, datetime

from fakeyou.db.voiceprint_store import VoiceprintRecord
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import (
    CalibrationArtifact,
    VerificationInputs,
    VerifiedDecision,
    decide_verified,
)
from fakeyou.identity.voiceprint_embed import SpeakerEmbedder
from fakeyou.identity.voiceprint_match import VoiceScores
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from tests._voiceprint_fakes import InertExtractor

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


CONSENT = datetime(2026, 10, 7, 10, 0, 0, tzinfo=UTC)


ENROLLED_AT = datetime(2026, 10, 7, 10, 0, 1, tzinfo=UTC)


LAST_USED_AT = datetime(2026, 10, 7, 10, 0, 2, tzinfo=UTC)


def fake_embedder(model_id: str | None = MODEL_VERSION) -> SpeakerEmbedder:
    return SpeakerEmbedder(InertExtractor(), model_id=model_id)


def named_registry(name: str) -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": name}])
    return registry


def stored_record(
    person_id: str,
    owner_name: str,
    embedding: tuple[float, ...],
    consent_ts: datetime = CONSENT,
) -> VoiceprintRecord:
    return VoiceprintRecord(
        person_id=person_id,
        embedding=embedding,
        dim=len(embedding),
        n_utterances=3,
        consent_ts=consent_ts,
        policy_version="v1",
        is_negative=False,
        enrolled_at=ENROLLED_AT,
        last_used_at=LAST_USED_AT,
        owner_name=owner_name,
    )


def owner_scores(owner_name: str | None, consent_ts: datetime | None) -> VoiceScores:
    return VoiceScores("spk-1", 0.9, 0.1, "verified", owner_name=owner_name, consent_ts=consent_ts)


def resolved_registry() -> SpeakerRegistry:
    registry = SpeakerRegistry()
    registry.update_roster(["dev-a"])
    registry.observe_stream(0, [11])
    registry.resolve(0)
    registry.note_roster([{"id": "dev-a", "name": "alice"}])
    return registry


def decide_claim(
    registry: SpeakerRegistry,
    diagnostics: Diagnostics,
    scores: VoiceScores,
    claim: str | None,
    *,
    overlap: bool | None = None,
) -> VerifiedDecision | None:
    row = registry.resolve(0)
    assert not isinstance(row, str)
    return decide_verified(
        row.uuid,
        scores,
        VerificationInputs(
            artifact=ARTIFACT, claim=claim, overlap=overlap, slot=row.slot, embedder=fake_embedder()
        ),
        registry,
        diagnostics,
    )


def misbind_kinds(faults: FaultReporter) -> set[str]:
    report = faults.report()
    assert report is not None
    return {entry["kind"] for entry in report["faults"]["kinds"]}


DIGEST_A = "a" * 64


DIGEST_B = "b" * 64


def stamped_artifact(model_version: str | None) -> CalibrationArtifact:
    return CalibrationArtifact(
        theta_llr=ARTIFACT.theta_llr,
        margin_floor=ARTIFACT.margin_floor,
        snorm_cohort=ARTIFACT.snorm_cohort,
        platt_a=ARTIFACT.platt_a,
        platt_b=ARTIFACT.platt_b,
        platt_version=ARTIFACT.platt_version,
        report_id=ARTIFACT.report_id,
        model_version=model_version,
    )


def passing_scores() -> VoiceScores:
    return VoiceScores("alice", 0.9, 0.1, "verified", owner_name="alice", consent_ts=CONSENT)
