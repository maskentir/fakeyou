from typing import Any

import pytest

from fakeyou.foundation.publication import PublicationDecision, PublicationValidity
from fakeyou.observability.participant_audio import (
    ConsentReference,
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


QUERIED_AT = 101.2
REFERENCE = ConsentReference(99.89, QUERIED_AT, "fake")


def measured(
    observer: ScriptedParticipantObserver | None,
    decision: PublicationDecision | None = ALLOWED,
    **options: Any,
) -> dict[str, Any]:
    return measure_participant_publication(
        _binding(),
        observer,
        decision=decision,
        clock_domain="fake",
        **{"post_validations": _post(), "queried_at": QUERIED_AT, **options},
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

    observation = observe_participant(_binding(), observer, REFERENCE)

    assert observation.publication is None
    assert observer.heard_calls == 0


INCOMPLETE_GRANTS: list[tuple[ParticipantConsent, ParticipantGap]] = [
    (granted(granted_at=None), "participant_consent_not_in_force"),
    (granted(at=float("nan")), "participant_consent_not_in_force"),
    (granted(at=float("inf")), "participant_consent_not_in_force"),
    (granted(source=""), "participant_consent_unknown"),
    (granted(scope="marketing"), "participant_consent_scope_mismatch"),
    (granted(revoked_at=10.0), "participant_consent_revoked"),
    (granted(revoked_at=5.0), "participant_consent_revoked"),
    (granted(revoked_at=float("nan")), "participant_consent_revoked"),
    (granted(observer_id="observer-2"), "participant_observer_mismatch"),
]


@pytest.mark.parametrize(("consent", "gap"), INCOMPLETE_GRANTS)
def test_observer_is_not_asked_what_it_heard_without_a_provable_grant(
    consent: ParticipantConsent, gap: ParticipantGap
) -> None:
    observer = ScriptedParticipantObserver(consented=consent)

    result = measured(observer)

    assert (result["status"], result["reason"]) == ("not_measurable", gap)
    assert observer.heard_calls == 0


@pytest.mark.parametrize("granted_at", [99.895, 99.95, 100.0, 100.5, 101.1, 150.0])
def test_grant_after_the_earliest_possible_audio_start_never_reads_the_observer(
    granted_at: float,
) -> None:
    observer = ScriptedParticipantObserver(consented=granted(at=granted_at))

    result = measured(observer)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_consent_not_in_force",
    )
    assert observer.heard_calls == 0


def test_grant_at_the_earliest_possible_audio_start_is_in_force() -> None:
    observer = ScriptedParticipantObserver(consented=granted(at=99.89))

    assert measured(observer)["status"] == "observed_compliant"
    assert observer.heard_calls == 1


def test_gate_uncertainty_moves_the_earliest_possible_audio_start_back() -> None:
    uncertain = PublicationDecision("valid", "ok", 99.9, "fake", 0.04)
    observer = ScriptedParticipantObserver(consented=granted(at=99.88))

    result = measured(observer, uncertain)

    assert result["reason"] == "participant_consent_not_in_force"
    assert observer.heard_calls == 0


UNPROVEN_REFERENCES: list[tuple[dict[str, Any], ParticipantGap]] = [
    ({"decision": None}, "participant_consent_reference_missing"),
    (
        {"decision": PublicationDecision("valid", "ok", None, "fake")},
        "participant_consent_reference_missing",
    ),
    (
        {"decision": PublicationDecision("valid", "ok", float("nan"), "fake")},
        "participant_consent_reference_missing",
    ),
    ({"queried_at": None}, "participant_consent_reference_missing"),
    ({"queried_at": float("inf")}, "participant_consent_reference_missing"),
    ({"queried_at": 99.0}, "participant_consent_reference_missing"),
    (
        {"decision": PublicationDecision("valid", "ok", 99.9, None)},
        "participant_consent_clock_incomparable",
    ),
    (
        {"decision": PublicationDecision("valid", "ok", 99.9, "page_wall_utc")},
        "participant_consent_clock_incomparable",
    ),
]


@pytest.mark.parametrize(("options", "gap"), UNPROVEN_REFERENCES)
def test_observer_is_not_read_without_a_comparable_trusted_reference(
    options: dict[str, Any], gap: ParticipantGap
) -> None:
    observer = ScriptedParticipantObserver()

    result = measured(observer, **options)

    assert result["participant_gap"] == gap
    assert result["status"] == "not_measurable"
    assert "compliant" not in result
    assert observer.heard_calls == 0


def test_consent_without_a_clock_domain_cannot_be_compared() -> None:
    observer = ScriptedParticipantObserver(consented=granted(clock_domain=""))

    result = measured(observer)

    assert result["reason"] == "participant_consent_clock_incomparable"
    assert observer.heard_calls == 0


def test_observer_clock_in_another_domain_than_the_consent_is_not_used() -> None:
    observer = ScriptedParticipantObserver(clock_domain="observer_wall")

    result = measured(observer)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_consent_clock_incomparable",
    )


def test_consent_revoked_after_the_sentence_ended_still_covers_it() -> None:
    observer = ScriptedParticipantObserver(consented=granted(revoked_at=101.5))

    assert measured(observer)["status"] == "observed_compliant"
    assert observer.heard_calls == 1


@pytest.mark.parametrize("queried_at", [101.5, 102.0, 200.0])
def test_no_new_read_is_made_once_consent_has_been_revoked(queried_at: float) -> None:
    observer = ScriptedParticipantObserver(consented=granted(revoked_at=101.5))

    result = measured(observer, queried_at=queried_at)

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "participant_consent_revoked",
    )
    assert observer.heard_calls == 0


def test_revocation_during_the_sentence_discards_a_report_read_before_it() -> None:
    observer = ScriptedParticipantObserver(consented=granted(revoked_at=100.6))

    result = measured(observer, queried_at=100.55)

    assert result["reason"] == "participant_consent_revoked"
    assert "compliant" not in result


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


@pytest.mark.parametrize("outcome", ["prevented", "dropped_before_audio"])
def test_caller_cannot_override_a_receipt_that_reports_no_local_write(
    outcome: PlaybackOutcome,
) -> None:
    result = measured(
        ScriptedParticipantObserver(),
        vetoed(),
        receipt=receipt(outcome),
        local_write_observed=True,
    )

    assert (result["status"], result["reason"]) == (
        "not_measurable",
        "publication_observation_conflict",
    )
    assert "compliant" not in result


def test_caller_cannot_hide_a_receipt_that_reports_a_local_write() -> None:
    result = measured(
        ScriptedParticipantObserver(),
        vetoed(),
        receipt=receipt("completed"),
        local_write_observed=False,
    )

    assert result["status"] == "observed_noncompliant"


def test_caller_value_is_used_only_when_no_matching_receipt_settled() -> None:
    for unsettled in (None, receipt("attempted"), receipt("prevented", "other-pub")):
        result = measured(None, vetoed(), receipt=unsettled, local_write_observed=False)
        assert result["status"] == "prevented_invalid"


def test_local_receipt_alone_never_yields_a_participant_verdict() -> None:
    for outcome in ("audio_started", "completed"):
        result = measured(None, receipt=receipt(outcome))
        assert result["status"] == "not_measurable"
