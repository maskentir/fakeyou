import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from evals._measurement_budget import BudgetLimits, MeasurementLedger
from evals._publication_measurement import (
    AudioPublication,
    EvidenceBinding,
    PublicationProbe,
    SentenceBinding,
    Validation,
)
from fakeyou.perception.evidence import SnapshotRef

ROLES = frozenset({"vision", "reply"})
OBSERVED_AT = 95.0


def snapshot(sequence: int = 1) -> SnapshotRef:
    return SnapshotRef(
        "camera",
        "participant-1",
        "stream-1",
        2,
        sequence,
        3,
        datetime(2026, 1, 1, tzinfo=UTC),
    )


def limits() -> BudgetLimits:
    return BudgetLimits("phase-5a", 20, Decimal("1.00"), 2, ROLES)


async def test_a_fake_provider_attempt_joins_the_published_sentence(tmp_path: Path) -> None:
    ledger = MeasurementLedger(tmp_path / "ledger.json", limits())
    attempt = ledger.reserve("vision", "fake", "model", Decimal("0.01"), turn_id="turn-1")
    ledger.mark_sent(attempt)
    ledger.settle(attempt, usd=Decimal("0.01"), inference=True, outcome="ok")

    evidence = EvidenceBinding(
        "camera", "gather-1", snapshot(), OBSERVED_AT, "fake", attempt, "inference"
    )
    binding = SentenceBinding(
        "pub-1",
        "meeting-1",
        "turn-1",
        "transcript-1",
        "request-1",
        0,
        "hash-1",
        "gather-1",
        ("camera",),
        (evidence,),
        "current",
    )
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding)

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        return Validation(
            99.9,
            "fake",
            snapshot(2),
            supported=True,
            unchanged=True,
            observed_at=OBSERVED_AT,
            uncertainty_seconds=0.01,
        )

    async def emit() -> AudioPublication:
        return AudioPublication(
            "pub-1", "transcript-1", 0, "hash-1", 100.0, 101.0, "fake", "participant_audible", 0.01
        )

    report = await probe.publish("pub-1", revalidate=revalidate, emit=emit)

    assert report["measurable"] is True
    assert report["evidence"][0]["provider_attempt_id"] == attempt
    assert report["gather_attempt_id"] == "gather-1"
    assert report["turn_id"] == "turn-1"
    json.dumps(report)

    state = ledger.snapshot()
    assert state["attempt_count"] == 1
    assert state["known_usd"] == "0.01"


async def test_a_superseded_attempt_stays_visible_without_claiming_the_sentence(
    tmp_path: Path,
) -> None:
    ledger = MeasurementLedger(tmp_path / "ledger.json", limits())
    superseded = ledger.reserve("vision", "fake", "a", Decimal("0.01"), turn_id="turn-1")
    ledger.mark_sent(superseded)
    ledger.settle(superseded, usd=Decimal("0.01"), inference=True, outcome="ok")
    used = ledger.reserve(
        "vision", "fake", "b", Decimal("0.01"), turn_id="turn-1", parent_attempt_id=superseded
    )
    ledger.mark_sent(used)
    ledger.settle(used, usd=Decimal("0.01"), inference=True, outcome="ok")

    evidence = EvidenceBinding(
        "camera", "gather-1", snapshot(), OBSERVED_AT, "fake", used, "inference"
    )
    binding = SentenceBinding(
        "pub-1",
        "meeting-1",
        "turn-1",
        "transcript-1",
        "request-1",
        0,
        "hash-1",
        "gather-1",
        ("camera",),
        (evidence,),
        "current",
    )
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding)

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        return Validation(
            99.9,
            "fake",
            snapshot(2),
            supported=True,
            unchanged=True,
            observed_at=OBSERVED_AT,
            uncertainty_seconds=0.01,
        )

    async def emit() -> AudioPublication:
        return AudioPublication(
            "pub-1", "transcript-1", 0, "hash-1", 100.0, 101.0, "fake", "participant_audible", 0.01
        )

    report = await probe.publish("pub-1", revalidate=revalidate, emit=emit)

    assert report["measurable"] is True
    assert report["evidence"][0]["provider_attempt_id"] == used

    state = ledger.snapshot()
    assert state["attempt_count"] == 2
    assert state["attempts"][1]["parent_attempt_id"] == superseded
    assert state["attempts"][0]["id"] == superseded
