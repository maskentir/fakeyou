from dataclasses import replace
from datetime import UTC, datetime

import pytest

from fakeyou.foundation.publication import (
    AudioPublication,
    EvidenceBinding,
    PublicationDecision,
    PublicationValidity,
    SentenceBinding,
    SnapshotRef,
    Validation,
)
from fakeyou.observability.publication_measurement import measure_publication


def _binding(capture: float | None = 95.1) -> SentenceBinding:
    snap = SnapshotRef("camera", "subject", "stream", 1, 1, 1, datetime.now(UTC))
    item = EvidenceBinding(
        "participant",
        "gather",
        snap,
        95.0,
        "provider",
        "attempt",
        "inference",
        capture_monotonic=capture,
        capture_clock_domain="utc",
        capture_mapping_clock_domain="fake",
        capture_time_origin="source_capture",
        capture_mapping_uncertainty_seconds=0.01,
        observation_clock_domain="fake",
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
        ("participant",),
        (item,),
        "current",
    )


def _publication() -> AudioPublication:
    return AudioPublication(
        "pub",
        "transcript",
        0,
        "hash",
        100.0,
        101.0,
        "fake",
        "participant_audible",
        0.01,
        participant_consent=True,
    )


def _post(
    *,
    checked_at: float = 102.0,
    supported: bool | None = True,
    uncertainty: float = 0.01,
    snap: SnapshotRef | None = None,
) -> dict[str, Validation]:
    snap = _binding().evidence[0].snapshot if snap is None else snap
    return {
        "participant": Validation(
            checked_at,
            "fake",
            snap,
            supported=supported,
            unchanged=True,
            observed_at=95.0,
            uncertainty_seconds=uncertainty,
        )
    }


def test_prevention_requires_explicit_no_write_receipt() -> None:
    result = measure_publication(
        _binding(),
        None,
        decision=PublicationDecision("invalid", "stale", 99.0, "fake"),
        clock_domain="fake",
        local_write_observed=False,
    )
    assert result["status"] == "prevented_invalid"


def test_unknown_gate_without_receipt_is_not_measurable() -> None:
    result = measure_publication(
        _binding(),
        None,
        decision=PublicationDecision("unknown", "timeout", 99.0, "fake"),
        clock_domain="fake",
    )
    assert result["status"] == "not_measurable"


def test_valid_observed_publication_requires_post_continuity() -> None:
    decision = PublicationDecision("valid", "ok", 99.9, "fake", 0.01)
    result = measure_publication(
        _binding(), _publication(), decision=decision, clock_domain="fake", post_validations=_post()
    )
    assert result["status"] == "observed_compliant"


def test_missing_capture_mapping_is_not_measurable() -> None:
    binding = _binding(None)
    result = measure_publication(
        binding,
        _publication(),
        decision=PublicationDecision("valid", "ok", 99.9, "fake"),
        clock_domain="fake",
        post_validations=_post(),
    )
    assert result["status"] == "not_measurable"


def test_missing_observer_and_local_write_are_not_compliant() -> None:
    result = measure_publication(
        _binding(),
        None,
        decision=PublicationDecision("valid", "ok", 99.9, "fake"),
        clock_domain="fake",
        local_write_observed=True,
    )
    assert result["status"] == "not_measurable"


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("stale", "observed_noncompliant"),
        ("consent", "not_measurable"),
        ("uncertain", "not_measurable"),
        ("nan", "not_measurable"),
        ("identity", "not_measurable"),
    ],
)
def test_observation_measurement_rejects_unsafe_observer_data(case: str, reason: str) -> None:
    publication = _publication()
    if case == "stale":
        publication = replace(publication, started_at=110.0, ended_at=111.0)
    elif case == "consent":
        publication = replace(publication, participant_consent=None)
    elif case == "uncertain":
        publication = replace(publication, uncertainty_seconds=0.06)
    elif case == "nan":
        publication = replace(publication, uncertainty_seconds=float("nan"))
    else:
        publication = replace(publication, transcript_id="other")
    result = measure_publication(
        _binding(),
        publication,
        decision=PublicationDecision("valid", "ok", 99.9, "fake", 0.01),
        clock_domain="fake",
        post_validations=_post(),
    )
    assert result["status"] == reason


def test_scene_change_after_publication_is_noncompliant() -> None:
    changed = SnapshotRef("camera", "subject", "stream", 1, 2, 2, datetime.now(UTC))
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision("valid", "ok", 99.9, "fake", 0.01),
        clock_domain="fake",
        post_validations=_post(snap=changed),
    )
    assert result["status"] == "observed_noncompliant"


def test_post_validation_uncertainty_and_interval_are_not_measurable() -> None:
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision("valid", "ok", 99.9, "fake", 0.01),
        clock_domain="fake",
        post_validations=_post(checked_at=100.5, uncertainty=0.06),
    )
    assert result["status"] == "not_measurable"


@pytest.mark.parametrize("checked_at", [None, 100.1, -1.0, float("nan"), 99.78])
def test_unproven_validation_time_never_becomes_compliant(checked_at: float | None) -> None:
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision("valid", "ok", checked_at, "fake", 0.05),
        clock_domain="fake",
        post_validations=_post(),
    )
    assert result["status"] == "not_measurable"
    assert result.get("compliant") is not True


@pytest.mark.parametrize("capture", [95.0, 100.1, float("nan")])
def test_unproven_capture_age_never_becomes_compliant(capture: float) -> None:
    result = measure_publication(
        _binding(capture),
        _publication(),
        decision=PublicationDecision("valid", "ok", 99.9, "fake"),
        clock_domain="fake",
        post_validations=_post(),
    )
    assert result["status"] == "not_measurable"
    assert result.get("compliant") is not True


def test_one_observation_cannot_satisfy_two_identical_sentence_attempts() -> None:
    first = _binding()
    replay = replace(first, publication_id="replay")
    decision = PublicationDecision("valid", "ok", 99.9, "fake")
    first_result = measure_publication(
        first, _publication(), decision=decision, clock_domain="fake", post_validations=_post()
    )
    wrong_result = measure_publication(
        replay, _publication(), decision=decision, clock_domain="fake", post_validations=_post()
    )
    replay_result = measure_publication(
        replay,
        replace(_publication(), publication_id="replay"),
        decision=decision,
        clock_domain="fake",
        post_validations=_post(),
    )
    assert first_result["status"] == replay_result["status"] == "observed_compliant"
    assert wrong_result["status"] == "not_measurable"
    assert wrong_result["reason"] == "publication_identity_mismatch"


def test_post_validation_clock_uncertainty_must_cover_audible_end() -> None:
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision("valid", "ok", 99.9, "fake"),
        clock_domain="fake",
        post_validations=_post(checked_at=101.005),
    )
    assert result["status"] == "not_measurable"


def test_unknown_visual_prevention_keeps_its_distinct_outcome() -> None:
    result = measure_publication(
        _binding(),
        None,
        decision=PublicationDecision("unknown", "timeout", None, "fake"),
        clock_domain="fake",
        local_write_observed=False,
    )
    assert result["status"] == "prevented_unknown"
    assert result.get("compliant") is not True


@pytest.mark.parametrize("validity", ["invalid", "unknown"])
@pytest.mark.parametrize("local_write", [True, None])
def test_vetoed_clip_heard_by_participant_is_observed_violation(
    validity: PublicationValidity, *, local_write: bool | None
) -> None:
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision(validity, "scene_changed", 99.9, "fake"),
        clock_domain="fake",
        local_write_observed=local_write,
    )
    assert result["status"] == "observed_noncompliant"
    assert result["compliant"] is False
    assert result["reason"] == f"published_despite_gate_{validity}"
    assert result["gate_reason"] == "scene_changed"


def test_vetoed_clip_without_any_observation_is_not_measurable() -> None:
    result = measure_publication(
        _binding(),
        None,
        decision=PublicationDecision("invalid", "scene_changed", 99.9, "fake"),
        clock_domain="fake",
    )
    assert result["status"] == "not_measurable"
    assert result["reason"] == "publication_outcome_unknown"


def test_vetoed_clip_with_conflicting_local_receipt_is_not_measurable() -> None:
    result = measure_publication(
        _binding(),
        _publication(),
        decision=PublicationDecision("invalid", "scene_changed", 99.9, "fake"),
        clock_domain="fake",
        local_write_observed=False,
    )
    assert result["status"] == "not_measurable"
    assert result["reason"] == "publication_observation_conflict"


@pytest.mark.parametrize(
    ("observed", "reason"),
    [
        (replace(_publication(), participant_consent=None), "participant_consent_unknown"),
        (
            replace(_publication(), observation_kind="local_pcm_write"),
            "local_write_is_not_observation",
        ),
        (replace(_publication(), clock_domain="other"), "participant_clock_unmeasurable"),
        (replace(_publication(), transcript_id="other"), "publication_identity_mismatch"),
        (replace(_publication(), ended_at=99.0), "participant_interval_invalid"),
    ],
)
def test_vetoed_clip_with_insufficient_observation_is_not_measurable(
    observed: AudioPublication, reason: str
) -> None:
    result = measure_publication(
        _binding(),
        observed,
        decision=PublicationDecision("invalid", "scene_changed", 99.9, "fake"),
        clock_domain="fake",
        local_write_observed=True,
    )
    assert result["status"] == "not_measurable"
    assert result["reason"] == reason
    assert result.get("compliant") is not False
