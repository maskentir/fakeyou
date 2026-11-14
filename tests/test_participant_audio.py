from typing import Any

import pytest

from fakeyou.foundation.publication import PublicationDecision, PublicationValidity
from fakeyou.observability.participant_audio import (
    ParticipantConsent,
    ParticipantGap,
    local_write_observed,
    measure_participant_publication,
    observe_participant,
)
from fakeyou.speech.contract import PlaybackOutcome, PlaybackReceipt
from tests._participant_audio_fakes import (
    OBSERVER_ID,
    ScriptedParticipantObserver,
    granted,
    observer_clock,
)
from tests.test_publication_measurement import _binding, _post

ALLOWED = PublicationDecision("valid", "ok", 99.9, "fake", 0.01)


def vetoed(validity: PublicationValidity = "invalid") -> PublicationDecision:
    return PublicationDecision(validity, "scene_changed", 99.9, "fake")


def receipt(outcome: PlaybackOutcome, publication_id: str = "pub") -> PlaybackReceipt:
    return PlaybackReceipt("attempt-1", outcome, "Sentence.", publication_id=publication_id)


def measured(
    observer: ScriptedParticipantObserver | None,
    decision: PublicationDecision = ALLOWED,
    **options: Any,
) -> dict[str, Any]:
    return measure_participant_publication(
        _binding(),
        observer,
        decision=decision,
        clock_domain="fake",
        post_validations=_post(),
        **options,
    )


def test_consented_mapped_observation_of_an_allowed_sentence_is_compliant() -> None:
    result = measured(ScriptedParticipantObserver())

    assert result["status"] == "observed_compliant"
    assert result["participant_gap"] is None
    assert result["participant_observer_id"] == OBSERVER_ID


def test_no_observer_is_reported_as_unavailable_never_compliant() -> None:
    result = measured(None, receipt=receipt("completed"))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_observer_unavailable",
    )
    assert result["participant_observer_id"] is None
    assert result.get("compliant") is not True


CONSENT_GAPS: list[tuple[ParticipantConsent, ParticipantGap]] = [
    (granted(state="unknown"), "participant_consent_unknown"),
    (granted(state="denied"), "participant_consent_denied"),
    (granted(state="revoked"), "participant_consent_revoked"),
    (granted(source=""), "participant_consent_unknown"),
    (granted(scope="marketing"), "participant_consent_scope_mismatch"),
    (granted(granted_at=None), "participant_consent_not_in_force"),
    (granted(at=100.5), "participant_consent_not_in_force"),
    (granted(revoked_at=100.5), "participant_consent_revoked"),
    (granted(revoked_at=101.0), "participant_consent_revoked"),
]


@pytest.mark.parametrize(("consent", "gap"), CONSENT_GAPS)
@pytest.mark.parametrize("decision", [ALLOWED, vetoed("invalid"), vetoed("unknown")])
def test_observation_without_consent_in_force_is_never_used(
    consent: ParticipantConsent, gap: ParticipantGap, decision: PublicationDecision
) -> None:
    result = measured(ScriptedParticipantObserver(consented=consent), decision)

    assert result["status"] == "not_measurable"
    assert result["reason"] == gap
    assert result["participant_gap"] == gap
    assert "compliant" not in result


@pytest.mark.parametrize("state", ["unknown", "denied", "revoked"])
def test_observer_is_not_asked_what_it_heard_without_standing_consent(state: str) -> None:
    observer = ScriptedParticipantObserver(consented=granted(state=state))

    observation = observe_participant(_binding(), observer)

    assert observation.publication is None
    assert observer.heard_calls == 0


def test_consent_revoked_after_the_sentence_ended_still_covers_it() -> None:
    result = measured(ScriptedParticipantObserver(consented=granted(revoked_at=101.5)))

    assert result["status"] == "observed_compliant"


def test_consent_for_another_observer_is_a_mismatch() -> None:
    result = measured(ScriptedParticipantObserver(consented=granted(observer_id="observer-2")))

    assert result["reason"] == "participant_observer_mismatch"


def test_sentence_reported_by_another_observer_is_a_mismatch() -> None:
    result = measured(ScriptedParticipantObserver(reported_by="observer-2"))

    assert result["reason"] == "participant_observer_mismatch"


@pytest.mark.parametrize("method", ["transcript", "timing_guess", ""])
def test_unverified_identity_method_is_not_an_observation(method: str) -> None:
    result = measured(ScriptedParticipantObserver(identity_method=method))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "publication_identity_unverified",
    )


def test_wrong_sentence_identity_is_a_mismatch_not_a_verdict() -> None:
    result = measured(ScriptedParticipantObserver(reported_hash="other"))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "publication_identity_mismatch",
    )


def test_unmapped_observer_clock_is_explicit() -> None:
    result = measured(ScriptedParticipantObserver(clock=None))

    assert (result["status"], result["reason"]) == ("not_measurable", "participant_clock_unmapped")


def test_stale_observer_clock_exchange_is_unmapped() -> None:
    result = measured(ScriptedParticipantObserver(clock=observer_clock(now=260.0)))

    assert result["reason"] == "participant_clock_unmapped"


def test_uncertain_observer_clock_is_unmeasurable() -> None:
    result = measured(ScriptedParticipantObserver(clock=observer_clock(round_trip=0.2)))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_clock_unmeasurable",
    )


def test_sentence_not_reported_for_an_allowed_clip_is_not_a_verdict() -> None:
    result = measured(ScriptedParticipantObserver(heard_from=None), receipt=receipt("completed"))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_sentence_not_reported",
    )


@pytest.mark.parametrize("validity", ["invalid", "unknown"])
def test_vetoed_sentence_heard_by_a_consenting_participant_is_a_violation(
    validity: PublicationValidity,
) -> None:
    result = measured(ScriptedParticipantObserver(), vetoed(validity))

    assert result["status"] == "observed_noncompliant"
    assert result["reason"] == f"published_despite_gate_{validity}"
    assert result["participant_gap"] is None


@pytest.mark.parametrize("outcome", ["prevented", "dropped_before_audio"])
def test_vetoed_sentence_with_a_no_write_receipt_and_no_report_is_prevented(
    outcome: PlaybackOutcome,
) -> None:
    result = measured(
        ScriptedParticipantObserver(heard_from=None), vetoed(), receipt=receipt(outcome)
    )

    assert result["status"] == "prevented_invalid"
    assert result["participant_gap"] == "participant_sentence_not_reported"


def test_vetoed_sentence_without_any_receipt_or_report_is_not_measurable() -> None:
    result = measured(ScriptedParticipantObserver(heard_from=None), vetoed())

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_sentence_not_reported",
    )


def test_vetoed_sentence_with_a_local_write_and_no_observer_is_not_measurable() -> None:
    result = measured(None, vetoed(), receipt=receipt("interrupted_after_audio"))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_observer_unavailable",
    )


def test_no_write_receipt_contradicted_by_the_participant_is_a_conflict() -> None:
    result = measured(ScriptedParticipantObserver(), vetoed(), receipt=receipt("prevented"))

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "publication_observation_conflict",
    )


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("attempted", None),
        ("prevented", False),
        ("dropped_before_audio", False),
        ("audio_started", True),
        ("interrupted_after_audio", True),
        ("completed", True),
    ],
)
def test_local_write_is_read_from_the_playback_receipt(
    outcome: PlaybackOutcome, *, expected: bool | None
) -> None:
    assert local_write_observed(receipt(outcome), _binding()) is expected


def test_receipt_of_another_publication_says_nothing_about_this_one() -> None:
    assert local_write_observed(receipt("prevented", "other-pub"), _binding()) is None
    assert local_write_observed(None, _binding()) is None


def test_local_receipt_alone_never_yields_a_participant_verdict() -> None:
    for outcome in ("audio_started", "completed"):
        result = measured(None, receipt=receipt(outcome))
        assert result["status"] == "not_measurable"
