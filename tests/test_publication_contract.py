from datetime import UTC, datetime

import pytest

from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationDecision,
    SentenceBinding,
    SnapshotRef,
    Validation,
    evaluate_visual_publication,
)


def _snapshot(sequence: int = 1) -> SnapshotRef:
    return SnapshotRef("camera", "subject", "stream", 1, sequence, 1, datetime.now(UTC))


def _binding(*, capture_monotonic: float | None = 98.0) -> SentenceBinding:
    item = EvidenceBinding(
        "camera",
        "gather",
        _snapshot(),
        95.0,
        None,
        None,
        "inference",
        capture_monotonic=capture_monotonic,
        capture_clock_domain="mono",
        capture_mapping_clock_domain="mono",
        capture_time_origin="source_capture",
        capture_mapping_uncertainty_seconds=0.01,
        observation_clock_domain="wall",
    )
    return SentenceBinding(
        "pub",
        "meeting",
        "turn",
        "transcript",
        "request",
        0,
        "hash",
        "gather",
        ("camera",),
        (item,),
        "current",
    )


@pytest.mark.asyncio
async def test_current_publication_requires_mapped_capture_time() -> None:
    async def revalidate(_item: EvidenceBinding) -> Validation:
        return Validation(
            99.9,
            "mono",
            _snapshot(2),
            supported=True,
            unchanged=True,
            observed_at=95.0,
            uncertainty_seconds=0.01,
        )

    decision = await evaluate_visual_publication(
        _binding(capture_monotonic=None), revalidate, now=lambda: 100.0, clock_domain="mono"
    )
    assert decision.validity == "unknown"


@pytest.mark.asyncio
async def test_historical_publication_does_not_require_current_capture_liveness() -> None:
    item = _binding(capture_monotonic=None)
    historical = SentenceBinding(
        item.publication_id,
        item.meeting_id,
        item.turn_id,
        item.transcript_id,
        item.request_id,
        item.sentence_index,
        item.sentence_hash,
        item.gather_attempt_id,
        item.required_kinds,
        item.evidence,
        "historical",
    )
    called = False

    async def revalidate(_item: EvidenceBinding) -> Validation:
        nonlocal called
        called = True
        raise AssertionError

    decision = await evaluate_visual_publication(
        historical, revalidate, now=lambda: 100.0, clock_domain="mono"
    )
    assert decision.allows
    assert called is False


def test_binding_rejects_empty_identity_unknown_scope_and_kind_mismatch() -> None:
    item = _binding().evidence[0]
    with pytest.raises(ValueError, match="identity"):
        SentenceBinding(
            "",
            "meeting",
            "turn",
            "transcript",
            "request",
            0,
            "hash",
            "gather",
            ("camera",),
            (item,),
            "current",
        )
    with pytest.raises(ValueError, match="scope"):
        SentenceBinding(
            "pub",
            "meeting",
            "turn",
            "transcript",
            "request",
            0,
            "hash",
            "gather",
            ("camera",),
            (item,),
            "future",
        )
    with pytest.raises(ValueError, match="covered"):
        SentenceBinding(
            "pub",
            "meeting",
            "turn",
            "transcript",
            "request",
            0,
            "hash",
            "gather",
            ("screen",),
            (item,),
            "current",
        )


def test_decision_allows_only_valid() -> None:
    assert PublicationDecision("valid", "ok", None, "mono").allows
    assert not PublicationDecision("unknown", "pending", None, "mono").allows
