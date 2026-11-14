import json
from collections.abc import Awaitable, Callable
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

CURRENT = SnapshotRef(
    "camera",
    "participant-1",
    "stream-1",
    2,
    2,
    3,
    datetime(2026, 1, 1, tzinfo=UTC),
)


def snapshot(sequence: int = 1) -> SnapshotRef:
    return SnapshotRef(
        "camera", "participant-1", "stream-1", 2, sequence, 3, datetime(2026, 1, 1, tzinfo=UTC)
    )


def evidence(observed_at: float = 95.0, gather_attempt_id: str = "gather-1") -> EvidenceBinding:
    return EvidenceBinding(
        "camera", gather_attempt_id, snapshot(), observed_at, "fake", "provider-1", "inference"
    )


def binding(
    *,
    publication_id: str = "pub-1",
    scope: str = "current",
    transcript_id: str = "transcript-1",
    sentence_hash: str = "hash-1",
    items: tuple[EvidenceBinding, ...] | None = None,
) -> SentenceBinding:
    observed = (evidence(),) if items is None else items
    return SentenceBinding(
        publication_id,
        "meeting-1",
        "turn-1",
        transcript_id,
        "request-1",
        0,
        sentence_hash,
        "gather-1",
        ("camera",) if observed else (),
        observed,
        scope,
    )


def validation(
    checked_at: float,
    current: SnapshotRef | None = CURRENT,
    *,
    unchanged: bool | None = True,
    supported: bool = True,
    observed_at: float = 95.0,
    uncertainty: float = 0.01,
    clock_domain: str = "fake",
) -> Validation:
    return Validation(
        checked_at,
        clock_domain,
        current,
        supported=supported,
        unchanged=unchanged,
        observed_at=observed_at,
        uncertainty_seconds=uncertainty,
    )


def publication(
    *,
    publication_id: str = "pub-1",
    transcript_id: str = "transcript-1",
    sentence_hash: str = "hash-1",
    started_at: float = 100.0,
    ended_at: float = 101.0,
    observation_kind: str = "participant_audible",
    uncertainty: float = 0.01,
    clock_domain: str = "fake",
) -> AudioPublication:
    return AudioPublication(
        publication_id,
        transcript_id,
        0,
        sentence_hash,
        started_at,
        ended_at,
        clock_domain,
        observation_kind,
        uncertainty,
    )


def revalidator(
    *, pre: Validation, post: Validation
) -> Callable[[EvidenceBinding], Awaitable[Validation]]:
    calls = 0

    async def revalidate(_evidence: EvidenceBinding) -> Validation:
        nonlocal calls
        calls += 1
        return pre if calls == 1 else post

    return revalidate


@pytest.mark.asyncio
async def test_publish_joins_exact_sentence_and_reports_json_safe_measurement() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is True
    assert report["compliant"] is True
    assert report["publication_id"] == "pub-1"
    json.dumps(report)


@pytest.mark.asyncio
async def test_evidence_stale_at_the_audible_start_is_measured_but_not_compliant() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(110.5))

    report = await probe.publish(
        "pub-1",
        revalidate=validate,
        emit=lambda: _async(publication(started_at=110.0, ended_at=111.0)),
    )

    assert report["measurable"] is True
    assert report["compliant"] is False
    assert "evidence_stale_at_publication" in report["violations"]
    assert "prepublication_validation_too_old" in report["violations"]


@pytest.mark.asyncio
async def test_a_stale_prepublication_validation_is_measured_but_not_compliant() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(90.0), post=validation(101.1))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is True
    assert report["compliant"] is False
    assert "prepublication_validation_too_old" in report["violations"]


@pytest.mark.asyncio
async def test_an_unknown_continuity_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1, unchanged=None))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "continuity_unknown"


@pytest.mark.asyncio
async def test_a_missing_current_snapshot_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1, current=None))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "current_snapshot_missing"


@pytest.mark.asyncio
async def test_an_uncertain_observation_is_measured_but_not_compliant() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(
        pre=validation(99.9, uncertainty=0.06), post=validation(101.1, uncertainty=0.06)
    )

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is True
    assert report["compliant"] is False
    assert "validation_uncertainty_too_large" in report["violations"]


@pytest.mark.asyncio
async def test_an_uncertain_publication_is_measured_but_not_compliant() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish(
        "pub-1",
        revalidate=validate,
        emit=lambda: _async(publication(uncertainty=0.06)),
    )

    assert report["measurable"] is True
    assert report["compliant"] is False
    assert "publication_uncertainty_too_large" in report["violations"]


@pytest.mark.asyncio
async def test_a_scene_change_during_the_audible_interval_is_measured_but_not_compliant() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(
        pre=validation(99.9), post=validation(101.1, snapshot(3), unchanged=False)
    )

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is True
    assert report["compliant"] is False
    assert "scene_not_continuous" in report["violations"]


@pytest.mark.asyncio
async def test_a_historical_frame_stays_measurable_and_compliant_when_it_is_old() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding(scope="historical", items=(evidence(observed_at=10.0),)))
    validate = revalidator(
        pre=validation(99.9, observed_at=10.0),
        post=validation(101.1, observed_at=10.0, unchanged=None),
    )

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is True
    assert report["compliant"] is True


@pytest.mark.asyncio
async def test_an_observed_sentence_that_does_not_match_the_binding_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish(
        "pub-1",
        revalidate=validate,
        emit=lambda: _async(publication(transcript_id="transcript-2")),
    )

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "publication_identity_mismatch"


@pytest.mark.asyncio
async def test_a_no_visual_turn_cannot_claim_another_sentence() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(
        SentenceBinding(
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
    )
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish(
        "pub-no-visual",
        revalidate=validate,
        emit=lambda: _async(publication(publication_id="pub-no-visual")),
    )

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "publication_identity_mismatch"


@pytest.mark.asyncio
async def test_a_missing_publication_recording_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(None))

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "no_publication_observed"


@pytest.mark.asyncio
async def test_an_unsynchronised_clock_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish(
        "pub-1",
        revalidate=validate,
        emit=lambda: _async(publication(clock_domain="other")),
    )

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "clock_domain_mismatch"


@pytest.mark.asyncio
async def test_a_validation_after_the_audible_start_is_not_measurable() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(101.1), post=validation(101.1))

    report = await probe.publish("pub-1", revalidate=validate, emit=lambda: _async(publication()))

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "validation_after_publication"


@pytest.mark.asyncio
async def test_a_local_write_is_not_a_participant_audible_observation() -> None:
    probe = PublicationProbe(lambda: 100.0, "fake")
    probe.register(binding())
    validate = revalidator(pre=validation(99.9), post=validation(101.1))

    report = await probe.publish(
        "pub-1",
        revalidate=validate,
        emit=lambda: _async(publication(observation_kind="local_write")),
    )

    assert report["measurable"] is False
    assert report["compliant"] is None
    assert report["reason"] == "not_participant_audible"


def test_binding_rejects_duplicate_or_incomplete_visual_joins() -> None:
    with pytest.raises(ValueError, match="required visual kinds"):
        SentenceBinding(
            "pub-1", "m", "t", "tr", "r", 0, "h", "g", ("camera", "camera"), (), "current"
        )


def test_binding_rejects_two_observations_of_one_required_kind() -> None:
    with pytest.raises(ValueError, match="two observations"):
        SentenceBinding(
            "pub-1",
            "m",
            "t",
            "tr",
            "r",
            0,
            "h",
            "g",
            ("camera",),
            (evidence(), evidence()),
            "current",
        )


def test_binding_rejects_an_observation_from_another_gather_attempt() -> None:
    with pytest.raises(ValueError, match="gather attempt"):
        SentenceBinding(
            "pub-1",
            "m",
            "t",
            "tr",
            "r",
            0,
            "h",
            "g",
            ("camera",),
            (evidence(gather_attempt_id="gather-other"),),
            "current",
        )


def test_a_no_visual_sentence_carries_no_visual_evidence() -> None:
    with pytest.raises(ValueError, match="no visual evidence"):
        SentenceBinding(
            "pub-1", "m", "t", "tr", "r", 0, "h", None, ("camera",), (evidence(),), "no_visual"
        )


async def _async(value: AudioPublication | None) -> AudioPublication | None:
    return value
