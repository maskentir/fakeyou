from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC, PAGE_CLOCK
from fakeyou.foundation.content_digest import ContentSample, DigestRecord, MatchLevel
from fakeyou.foundation.publication import (
    Dependence,
    EvidenceBinding,
    SnapshotRef,
    Validation,
)
from fakeyou.perception.inspector import VisualInspection
from fakeyou.perception.timeline import SCREEN_SOURCE_ID
from tests._perception_fakes import START, keyframe
from tests.test_publication_activation import (
    BETA_HISTORICAL,
    ROADMAP,
    Meeting,
    ask,
    show_budget,
    stop_sharing,
)
from tests.test_publication_freshness import (
    FRESH,
    OLD,
    confirm_same,
    evidence,
    outcome,
    sampled,
    validated,
)
from tests.test_revalidation_match import BUDGET, Scene

SHOWN = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
CAMERA = SnapshotRef("camera", "device-budi", "stream-budi", 0, 1, 0, SHOWN)


def camera_evidence(captured: float = FRESH) -> EvidenceBinding:
    return replace(evidence(captured), kind="participant", snapshot=CAMERA)


def camera_validated(
    match: MatchLevel | None = None, confirmation: ContentSample | None = None, **changes: Any
) -> Validation:
    return replace(validated(match, confirmation, **{"current_snapshot": CAMERA, **changes}))


INTERRUPTED = validated("exact", sampled(99.0, sequence=4, continuity=2))
SCREEN_ROWS: list[tuple[str, Validation, Dependence | None, tuple[str, str]]] = [
    ("P1 content", validated("exact", sampled(99.0, sequence=2)), "content", ("valid", "ok")),
    ("P1 source", validated("exact", sampled(99.0, sequence=2)), "source", ("valid", "ok")),
    ("P2 content", INTERRUPTED, "content", ("valid", "content_confirmed_after_interruption")),
    ("P2 source", INTERRUPTED, "source", ("unknown", "source_identity_changed")),
    ("N4 undeclared", INTERRUPTED, None, ("unknown", "source_identity_changed")),
    (
        "P3 content",
        validated("exact", sampled(99.0, sequence=2), source_changed=True),
        "content",
        ("valid", "content_confirmed_after_interruption"),
    ),
    (
        "P3 source",
        validated("exact", sampled(99.0, sequence=2), source_changed=True),
        "source",
        ("unknown", "source_identity_changed"),
    ),
    (
        "P5 content",
        validated(source_stopped=True, current_snapshot=None),
        "content",
        ("invalid", "scene_changed"),
    ),
    (
        "P5 source",
        validated(source_stopped=True, current_snapshot=None),
        "source",
        ("invalid", "scene_changed"),
    ),
    (
        "subject dependence on a screen is treated as source",
        INTERRUPTED,
        "subject",
        ("unknown", "source_identity_changed"),
    ),
]


@pytest.mark.parametrize(
    ("validation", "dependence", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in SCREEN_ROWS],
)
def test_screen_identity_table(
    validation: Validation, dependence: Dependence | None, expected: tuple[str, str]
) -> None:
    assert outcome(evidence(FRESH), validation, dependence) == expected


EXACT = sampled(99.0, sequence=2)
CAMERA_ROWS: list[tuple[str, EvidenceBinding, Validation, tuple[str, str]]] = [
    ("C1", camera_evidence(), camera_validated(), ("valid", "ok")),
    ("C2 fresh", camera_evidence(), camera_validated("exact", EXACT), ("valid", "ok")),
    (
        "C2 old",
        camera_evidence(OLD),
        camera_validated("exact", EXACT),
        ("valid", "continuity_confirmed"),
    ),
    (
        "C3 C4",
        camera_evidence(),
        camera_validated("similar", EXACT),
        ("unknown", "content_changed_unverified"),
    ),
    (
        "C5 gone",
        camera_evidence(),
        camera_validated(source_stopped=True, current_snapshot=None),
        ("invalid", "scene_changed"),
    ),
    (
        "C5 stream replaced with identical frames",
        camera_evidence(),
        camera_validated("exact", EXACT, unchanged=False),
        ("invalid", "scene_changed"),
    ),
    (
        "C6 with identical frames",
        camera_evidence(),
        camera_validated("exact", EXACT, subject_changed=True),
        ("invalid", "subject_changed"),
    ),
    ("C7 none", camera_evidence(OLD), camera_validated(), ("invalid", "capture_too_old")),
    (
        "C7 non-exact",
        camera_evidence(OLD),
        camera_validated("similar", EXACT),
        ("invalid", "capture_too_old"),
    ),
    (
        "C8",
        camera_evidence(),
        camera_validated(current_snapshot=None, unchanged=None),
        ("unknown", "scene_continuity_unknown"),
    ),
    (
        "C9",
        camera_evidence(),
        camera_validated("unknown", EXACT),
        ("unknown", "continuity_unknown"),
    ),
    (
        "content dependence declared on a camera claim is ignored",
        camera_evidence(),
        camera_validated("exact", sampled(99.0, sequence=4, continuity=2)),
        ("unknown", "source_identity_changed"),
    ),
]


@pytest.mark.parametrize(
    ("item", "validation", "expected"),
    [pytest.param(*row[1:], id=row[0]) for row in CAMERA_ROWS],
)
def test_camera_identity_table(
    item: EvidenceBinding, validation: Validation, expected: tuple[str, str]
) -> None:
    assert outcome(item, validation, "content") == expected


def test_camera_sample_keeps_its_own_capture_domain() -> None:
    item = camera_evidence()

    assert item.capture_clock_domain == PAGE_CLOCK
    assert item.capture_mapping_clock_domain == LOCAL_MONOTONIC


def paged(record: DigestRecord, stream_id: str, sequence: int) -> DigestRecord:
    return replace(record, stream_id=stream_id, sequence=sequence)


def test_same_content_from_another_stream_is_a_changed_source() -> None:
    scene = Scene()
    item = scene.screen_evidence()
    assert item.analyzed is not None
    first = paged(item.analyzed.record, "share-1", 2)
    scene.timeline.confirm(first, captured_at=START + timedelta(seconds=1), capture=None)
    bound = replace(item, analyzed=replace(item.analyzed, record=first))

    scene.timeline.confirm(
        paged(first, "share-2", 3), captured_at=START + timedelta(seconds=2), capture=None
    )

    validation = scene.revalidate.validate(bound)
    assert (validation.match, validation.source_changed) == ("exact", True)


def test_same_content_shown_by_another_presenter_is_a_changed_source() -> None:
    scene = Scene()
    item = scene.screen_evidence()
    budi = scene.timeline.participant("device-budi", "Budi")

    scene.timeline.show_screen(keyframe(ROADMAP, seconds=1.0), budi, data=ROADMAP)

    validation = scene.revalidate.validate(item)
    assert (validation.match, validation.source_changed) == ("exact", True)


def test_same_content_from_the_same_share_is_not_a_changed_source() -> None:
    scene = Scene()
    item = scene.screen_evidence()

    scene.show(ROADMAP, 1.0)

    validation = scene.revalidate.validate(item)
    assert (validation.match, validation.source_changed) == ("exact", False)


def test_content_that_returned_after_another_slide_is_a_changed_source() -> None:
    scene = Scene()
    item = scene.screen_evidence()
    scene.show(BUDGET, 1.0)

    scene.show(ROADMAP, 2.0)

    validation = scene.revalidate.validate(item)
    assert validation.match == "exact"
    assert item.analyzed is not None
    assert validation.confirmation is not None
    assert validation.confirmation.continuity != item.analyzed.continuity


def test_dropped_camera_is_a_stopped_source_and_a_stale_one_is_not() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    item = scene.camera_evidence()

    scene.clock.at = 111.0
    stale = scene.revalidate.validate(item)
    scene.timeline.camera_frames.drop("device-budi")
    gone = scene.revalidate.validate(item)

    assert (stale.source_stopped, stale.current_snapshot) == (False, None)
    assert gone.source_stopped


def test_camera_stream_that_now_belongs_to_another_participant_is_a_changed_subject() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    item = scene.camera_evidence()
    assert item.analyzed is not None

    scene.timeline.confirm(
        replace(item.analyzed.record, subject_id="device-ayu", sequence=1),
        captured_at=START + timedelta(seconds=1),
        capture=None,
    )

    assert scene.revalidate.validate(item).subject_changed


def test_replaced_camera_stream_of_the_same_participant_is_not_the_same_scene() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    item = scene.camera_evidence()

    scene.camera((120, 120, 120), 1.0, stream="other-stream")

    validation = scene.revalidate.validate(item)
    assert validation.match == "exact"
    assert validation.unchanged is False


async def test_restarted_share_with_the_identical_slide_does_not_prove_the_source() -> None:
    def restart(rig: Meeting) -> None:
        stop_sharing(rig)
        presenter = rig.timeline.participant("device-ayu", "Ayu")
        rig.timeline.show_screen(keyframe(ROADMAP, seconds=1.8), presenter, data=ROADMAP)

    meeting = Meeting(before_write=restart)

    turn = await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.prevented == "unknown:source_identity_changed"
    assert [item.text for item in turn.counted] == [BETA_HISTORICAL]


async def test_another_slide_is_a_changed_scene_end_to_end() -> None:
    meeting = Meeting(before_write=show_budget)

    await ask(meeting, "When is beta?", needs=("screen",))

    assert meeting.prevented == "invalid:scene_changed"


async def test_fresh_camera_claim_with_no_later_sample_is_published() -> None:
    meeting = Meeting(inspections=(VisualInspection(verdict="supported", answer="Grey."),))

    turn = await ask(
        meeting, "What is Budi wearing?", needs=("participant",), reply="Budi wears grey."
    )

    assert [item.outcome for item in meeting.settled] == ["completed"]
    assert len(turn.counted) == 1


async def test_static_slide_confirmation_keeps_the_source_for_a_source_claim() -> None:
    meeting = Meeting(
        look_at=129.0, write_at=130.0, before_write=lambda rig: confirm_same(rig, at=129.6)
    )

    await ask(meeting, "When is beta?", needs=("screen",))

    latest = meeting.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    assert [item.outcome for item in meeting.settled] == ["completed"]
