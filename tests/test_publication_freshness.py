from dataclasses import replace
from datetime import UTC, datetime

import pytest

from fakeyou.foundation.publication import (
    EvidenceBinding,
    SentenceBinding,
    SnapshotRef,
    Validation,
    evaluate_visual_publication,
)


def _evidence() -> EvidenceBinding:
    return EvidenceBinding(
        kind="screen",
        gather_attempt_id="gather",
        snapshot=SnapshotRef(
            "screen", "subject", "stream", 1, 1, 1, datetime(2026, 11, 14, tzinfo=UTC)
        ),
        observed_at=99.0,
        provider="fake",
        provider_attempt_id="provider",
        inference="inference",
        capture_monotonic=98.0,
        capture_clock_domain="utc",
        capture_mapping_clock_domain="test",
        capture_time_origin="source_capture",
        capture_mapping_uncertainty_seconds=0.0,
        observation_clock_domain="test",
    )


def _binding(*evidence: EvidenceBinding) -> SentenceBinding:
    return SentenceBinding(
        "publication",
        "meeting",
        "turn",
        "transcript",
        "request",
        0,
        "hash",
        "gather",
        tuple(item.kind for item in evidence),
        evidence,
        "current",
    )


@pytest.mark.parametrize("threshold", ["capture", "validation"])
async def test_threshold_crossing_uncertainty_is_unknown(threshold: str) -> None:
    evidence = _evidence()
    if threshold == "capture":
        evidence = replace(
            evidence, capture_monotonic=95.02, capture_mapping_uncertainty_seconds=0.05
        )

    async def validate(item: EvidenceBinding) -> Validation:
        return Validation(
            checked_at=99.78 if threshold == "validation" else 99.9,
            clock_domain="test",
            current_snapshot=item.snapshot,
            supported=True,
            unchanged=True,
            observed_at=99.0,
            uncertainty_seconds=0.05 if threshold == "validation" else 0.0,
        )

    decision = await evaluate_visual_publication(
        _binding(evidence), validate, now=lambda: 100.0, clock_domain="test"
    )
    assert decision.validity == "unknown"


async def test_last_revalidation_cannot_refresh_first_capture_age() -> None:
    clock = [99.9]
    first = replace(_evidence(), capture_monotonic=94.95)
    snapshot = first.snapshot
    assert snapshot is not None
    second = replace(_evidence(), kind="participant", snapshot=replace(snapshot, source="camera"))

    async def validate(item: EvidenceBinding) -> Validation:
        if item.kind == "participant":
            clock[0] = 100.1
        return Validation(
            clock[0],
            "test",
            item.snapshot,
            supported=True,
            unchanged=True,
            observed_at=99.0,
            uncertainty_seconds=0.0,
        )

    decision = await evaluate_visual_publication(
        _binding(first, second), validate, now=lambda: clock[0], clock_domain="test"
    )
    assert decision.validity == "invalid"
    assert decision.reason == "capture_too_old"


async def test_valid_decision_preserves_actual_validation_time_and_uncertainty() -> None:
    async def validate(item: EvidenceBinding) -> Validation:
        return Validation(
            99.9,
            "test",
            item.snapshot,
            supported=True,
            unchanged=True,
            observed_at=99.0,
            uncertainty_seconds=0.03,
        )

    decision = await evaluate_visual_publication(
        _binding(_evidence()), validate, now=lambda: 100.0, clock_domain="test"
    )
    assert decision.validity == "valid"
    assert decision.checked_at == 99.9
    assert decision.uncertainty_seconds == 0.03
