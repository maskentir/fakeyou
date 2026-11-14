import json
from datetime import UTC, datetime

import pytest

from evals._publication_measurement import (
    AudioPublication,
    EvidenceBinding,
    PublicationProbe,
    SentenceBinding,
    Validation,
)
from fakeyou.perception.evidence import SnapshotRef


def snapshot(sequence: int = 1) -> SnapshotRef:
    return SnapshotRef(
        "camera", "participant-1", "stream-1", 2, sequence, 3, datetime(2026, 1, 1, tzinfo=UTC)
    )


def binding(*, publication_id: str = "pub-1", scope: str = "current") -> SentenceBinding:
    evidence = EvidenceBinding(
        "camera", "gather-1", snapshot(), 95.0, "fake", "provider-1", "inference"
    )
    return SentenceBinding(
        publication_id,
        "meeting-1",
        "turn-1",
        "transcript-1",
        "request-1",
        0,
        "hash-1",
        "gather-1",
        ("camera",),
        (evidence,),
        scope,
    )


def validation(
    checked_at: float, current: SnapshotRef | None = None, *, unchanged: bool | None = True
) -> Validation:
    return Validation(
        checked_at,
        "fake",
        current or snapshot(2),
        supported=True,
        unchanged=unchanged,
        observed_at=95.0,
        uncertainty_seconds=0.01,
    )


def publication(*, publication_id: str = "pub-1") -> AudioPublication:
    return AudioPublication(
        publication_id,
        "transcript-1",
        0,
        "hash-1",
        100.0,
        101.0,
        "fake",
        "participant_audible",
        0.01,
    )


@pytest.mark.asyncio
async def test_publish_joins_exact_sentence_and_reports_json_safe_measurement() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        return validation(99.9 if calls == 1 else 101.1)

    report = await probe.publish("pub-1", revalidate=revalidate, emit=lambda: _async(publication()))
    assert report["measurable"] is True
    assert report["publication_id"] == "pub-1"
    json.dumps(report)


@pytest.mark.asyncio
async def test_publish_rejects_stale_prepublication_validation() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        return validation(90.0 if calls == 1 else 101.1)

    report = await probe.publish("pub-1", revalidate=revalidate, emit=lambda: _async(publication()))
    assert report["measurable"] is False
    assert report["reason"] == "prepublication_validation_too_old"


@pytest.mark.asyncio
async def test_publish_rejects_scene_change_during_audible_interval() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        if calls == 1:
            return validation(99.9)
        return validation(101.1, snapshot(3), unchanged=False)

    report = await probe.publish("pub-1", revalidate=revalidate, emit=lambda: _async(publication()))
    assert report["measurable"] is False
    assert report["reason"] == "scene_not_continuous"


@pytest.mark.asyncio
async def test_historical_and_no_visual_scopes_do_not_claim_current_scene() -> None:
    historical = binding(scope="historical")
    no_visual = SentenceBinding(
        "pub-no-visual",
        "meeting-1",
        "turn-2",
        "transcript-2",
        "request-2",
        0,
        "same-text",
        None,
        (),
        (),
        "no_visual",
    )
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(historical)
    probe.register(no_visual)
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        return validation(99.9 if calls in (1, 3) else 101.1, unchanged=None)

    historical_report = await probe.publish(
        "pub-1", revalidate=revalidate, emit=lambda: _async(publication())
    )
    no_visual_report = await probe.publish(
        "pub-no-visual",
        revalidate=revalidate,
        emit=lambda: _async(publication(publication_id="pub-no-visual")),
    )
    assert historical_report["measurable"] is True
    assert no_visual_report["measurable"] is True


@pytest.mark.asyncio
async def test_publish_rejects_observed_sentence_identity_mismatch() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        return validation(99.9 if calls == 1 else 101.1)

    report = await probe.publish(
        "pub-1", revalidate=revalidate, emit=lambda: _async(publication(publication_id="other"))
    )
    assert report["measurable"] is False
    assert report["reason"] == "publication_identity_mismatch"


def test_binding_rejects_duplicate_or_incomplete_visual_joins() -> None:
    with pytest.raises(ValueError, match="required visual kinds"):
        SentenceBinding(
            "pub-1", "m", "t", "tr", "r", 0, "h", "g", ("camera", "camera"), (), "current"
        )


async def _async(value: AudioPublication) -> AudioPublication:
    return value
