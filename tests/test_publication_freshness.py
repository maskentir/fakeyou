from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC, PAGE_CLOCK, MappedInstant
from fakeyou.foundation.content_digest import ContentSample, MatchLevel
from fakeyou.foundation.publication import (
    EvidenceBinding,
    PublicationDecision,
    SentenceBinding,
    SnapshotRef,
    Validation,
    decide_visual_publication,
    sentence_digest,
)
from fakeyou.perception.timeline import SCREEN_SOURCE_ID
from tests._perception_fakes import START
from tests.test_content_digest import pixels
from tests.test_publication_activation import CAPTURED_AT, Meeting, ask

AT = 100.0
SHOWN = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
SCREEN = SnapshotRef("screen", "screen#1", "sha", 0, 0, 0, SHOWN)


def sampled(
    captured: float | None,
    *,
    sequence: int,
    uncertainty: float = 0.01,
    continuity: int = 0,
    domain: str = LOCAL_MONOTONIC,
) -> ContentSample:
    capture = None if captured is None else MappedInstant(captured, uncertainty, domain)
    return ContentSample(
        pixels(sequence=sequence), SHOWN, capture, revision=sequence, continuity=continuity
    )


def evidence(captured: float | None = 98.0, *, uncertainty: float = 0.01) -> EvidenceBinding:
    analyzed = sampled(captured, sequence=1, uncertainty=uncertainty)
    return EvidenceBinding(
        "screen",
        "gather",
        SCREEN,
        99.0,
        "fake/vision",
        "attempt",
        "inference",
        capture_monotonic=captured,
        capture_clock_domain=PAGE_CLOCK,
        capture_mapping_clock_domain=None if captured is None else LOCAL_MONOTONIC,
        capture_time_origin="page_clock_exchange",
        capture_mapping_uncertainty_seconds=None if captured is None else uncertainty,
        status="produced",
        analyzed=analyzed,
    )


def validated(
    match: MatchLevel | None = None, confirmation: ContentSample | None = None, **changes: Any
) -> Validation:
    validation = Validation(
        checked_at=AT - 0.01,
        clock_domain=LOCAL_MONOTONIC,
        current_snapshot=SCREEN,
        supported=True,
        unchanged=True,
        observed_at=AT - 0.01,
        uncertainty_seconds=0.0,
        match=match,
        confirmation=confirmation,
    )
    return replace(validation, **changes)


def decide(item: EvidenceBinding, validation: Validation) -> PublicationDecision:
    binding = SentenceBinding(
        "publication",
        "meeting",
        "turn",
        "turn",
        "request",
        0,
        sentence_digest("Beta is 17 March 2027."),
        "gather",
        ("screen",),
        (item,),
        "current",
    )
    return decide_visual_publication(
        binding, {"screen": validation}, at=AT, clock_domain=LOCAL_MONOTONIC
    )


def outcome(item: EvidenceBinding, validation: Validation) -> tuple[str, str]:
    decision = decide(item, validation)
    return decision.validity, decision.reason


FRESH = 98.0
OLD = 90.0
ROWS: list[tuple[str, EvidenceBinding, Validation, tuple[str, str]]] = [
    ("S1", evidence(FRESH), validated(), ("valid", "ok")),
    ("S2", evidence(FRESH), validated("exact", sampled(99.0, sequence=2)), ("valid", "ok")),
    (
        "S3",
        evidence(OLD),
        validated("exact", sampled(99.0, sequence=2)),
        ("valid", "continuity_confirmed"),
    ),
    ("S4", evidence(OLD), validated(), ("invalid", "capture_too_old")),
    (
        "S5",
        evidence(OLD),
        validated("exact", sampled(94.0, sequence=2)),
        ("invalid", "confirmation_too_old"),
    ),
    (
        "S6",
        evidence(OLD),
        validated("exact", sampled(95.0, sequence=2)),
        ("unknown", "confirmation_age_threshold_uncertain"),
    ),
    (
        "S7 fresh",
        evidence(FRESH),
        validated("similar", sampled(99.0, sequence=2)),
        ("unknown", "content_changed_unverified"),
    ),
    (
        "S7 old",
        evidence(OLD),
        validated("similar", sampled(99.0, sequence=2)),
        ("unknown", "content_changed_unverified"),
    ),
    (
        "S8",
        evidence(FRESH),
        validated("distinct", sampled(99.0, sequence=2), unchanged=False),
        ("invalid", "scene_changed"),
    ),
    (
        "S9",
        evidence(FRESH),
        validated("unknown", sampled(99.0, sequence=2)),
        ("unknown", "continuity_unknown"),
    ),
    (
        "S10",
        evidence(FRESH),
        validated(source_stopped=True, current_snapshot=None),
        ("invalid", "scene_changed"),
    ),
    ("S11", evidence(95.0), validated(), ("unknown", "capture_age_threshold_uncertain")),
    ("S12 no sample", evidence(None), validated(), ("unknown", "capture_time_unmapped")),
    (
        "S12 exact sample",
        evidence(None),
        validated("exact", sampled(99.0, sequence=2)),
        ("unknown", "capture_time_unmapped"),
    ),
    (
        "S13 unmapped",
        evidence(OLD),
        validated("exact", sampled(None, sequence=2)),
        ("unknown", "confirmation_time_unmapped"),
    ),
    (
        "S13 unmapped, fresh capture",
        evidence(FRESH),
        validated("exact", sampled(None, sequence=2)),
        ("unknown", "confirmation_time_unmapped"),
    ),
    (
        "S13 uncertain mapping",
        evidence(OLD),
        validated("exact", sampled(99.0, sequence=2, uncertainty=0.06)),
        ("unknown", "confirmation_time_unmapped"),
    ),
    (
        "S13 other clock domain",
        evidence(OLD),
        validated("exact", sampled(99.0, sequence=2, domain=PAGE_CLOCK)),
        ("unknown", "confirmation_time_unmapped"),
    ),
    (
        "S14 before the analyzed capture",
        evidence(OLD),
        validated("exact", sampled(89.0, sequence=2)),
        ("unknown", "confirmation_time_inconsistent"),
    ),
    (
        "S14 after the boundary",
        evidence(OLD),
        validated("exact", sampled(100.5, sequence=2)),
        ("unknown", "confirmation_time_inconsistent"),
    ),
    (
        "S16",
        evidence(OLD),
        validated("exact", sampled(99.0, sequence=4, continuity=2)),
        ("valid", "content_confirmed_after_interruption"),
    ),
    (
        "S18 with a later sample",
        replace(evidence(FRESH), analyzed=None),
        validated("unknown", sampled(99.0, sequence=2)),
        ("unknown", "continuity_unknown"),
    ),
    ("S18 without one", replace(evidence(FRESH), analyzed=None), validated(), ("valid", "ok")),
]


@pytest.mark.parametrize(
    ("item", "validation", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in ROWS],
)
def test_screen_content_decision_table(
    item: EvidenceBinding, validation: Validation, expected: tuple[str, str]
) -> None:
    assert outcome(item, validation) == expected


@pytest.mark.parametrize(
    ("age", "uncertainty", "expected"),
    [
        (4.94, 0.05, "valid"),
        (4.96, 0.05, "unknown"),
        (5.04, 0.05, "unknown"),
        (5.06, 0.05, "invalid"),
        (4.5, 0.06, "unknown"),
    ],
)
def test_capture_age_near_the_limit_is_never_valid_inside_its_uncertainty(
    age: float, uncertainty: float, expected: str
) -> None:
    decision = decide(evidence(AT - age, uncertainty=uncertainty), validated())

    assert decision.validity == expected


@pytest.mark.parametrize(
    ("age", "uncertainty", "expected"),
    [
        (4.94, 0.05, "valid"),
        (4.96, 0.05, "unknown"),
        (5.04, 0.05, "unknown"),
        (5.06, 0.05, "invalid"),
    ],
)
def test_confirmation_age_near_the_limit_is_never_valid_inside_its_uncertainty(
    age: float, uncertainty: float, expected: str
) -> None:
    confirmation = sampled(AT - age, sequence=2, uncertainty=uncertainty)

    assert decide(evidence(OLD), validated("exact", confirmation)).validity == expected


def test_decision_carries_both_samples_and_the_match_level() -> None:
    item = evidence(OLD)
    confirmation = sampled(99.0, sequence=4, continuity=2)

    [basis] = decide(item, validated("exact", confirmation)).basis

    assert basis.kind == "screen"
    assert basis.analyzed is item.analyzed
    assert basis.confirmation is confirmation
    assert basis.match == "exact"
    assert basis.source_continuous is False
    assert basis.evaluated_at == AT


def test_prevented_decision_also_carries_its_basis() -> None:
    [basis] = decide(evidence(OLD), validated("similar", sampled(99.0, sequence=2))).basis

    assert basis.match == "similar"
    assert basis.source_continuous is True


def test_confirmation_never_changes_the_analyzed_capture() -> None:
    item = evidence(OLD)
    before = replace(item)

    decide(item, validated("exact", sampled(99.0, sequence=2)))

    assert item == before
    assert item.capture_monotonic == OLD


def test_missing_validation_for_bound_evidence_is_unknown() -> None:
    binding = SentenceBinding(
        "publication",
        "meeting",
        "turn",
        "turn",
        "request",
        0,
        sentence_digest("Sentence."),
        "gather",
        ("screen",),
        (evidence(FRESH),),
        "current",
    )

    decision = decide_visual_publication(binding, {}, at=AT, clock_domain=LOCAL_MONOTONIC)

    assert (decision.validity, decision.reason) == ("unknown", "validation_missing")


def confirm_same(meeting: Meeting, *, at: float) -> None:
    latest = meeting.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    captured_at = START + timedelta(seconds=at - CAPTURED_AT)
    meeting.timeline.confirm(
        replace(latest.record, sequence=latest.record.sequence + 1),
        captured_at=captured_at,
        capture=meeting.page_clock.map(captured_at.timestamp() * 1000),
    )


@pytest.mark.parametrize("age", [5.5, 30.0, 300.0])
async def test_static_slide_with_a_fresh_exact_sample_is_published(age: float) -> None:
    written = CAPTURED_AT + age
    meeting = Meeting(
        look_at=written - 1.0,
        write_at=written,
        before_write=lambda rig: confirm_same(rig, at=written - 0.4),
    )

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    [binding] = turn.meeting.bindings
    [item] = binding.evidence
    assert item.capture_monotonic == pytest.approx(CAPTURED_AT, abs=0.01)
    assert [receipt.outcome for receipt in turn.meeting.settled] == ["completed"]
    assert len(turn.counted) == 1


async def test_static_slide_whose_last_exact_sample_is_stale_is_prevented() -> None:
    written = CAPTURED_AT + 30.0
    meeting = Meeting(
        look_at=written - 1.0,
        write_at=written,
        before_write=lambda rig: confirm_same(rig, at=written - 6.0),
    )

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert turn.meeting.prevented == "invalid:confirmation_too_old"


async def test_slow_reply_with_a_fresh_exact_sample_is_published() -> None:
    meeting = Meeting(
        look_at=CAPTURED_AT + 0.5,
        write_at=CAPTURED_AT + 5.2,
        before_write=lambda rig: confirm_same(rig, at=CAPTURED_AT + 5.0),
    )

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert [receipt.outcome for receipt in turn.meeting.settled] == ["completed"]
