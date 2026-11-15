from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest

from fakeyou.foundation.content_digest import (
    DECODED_PIXELS_SHA256,
    DigestRecord,
    MatchLevel,
)
from fakeyou.foundation.publication import EvidenceBinding, SnapshotRef
from fakeyou.perception.context import PerceptionContext
from fakeyou.perception.revalidation import SceneRevalidator
from fakeyou.perception.timeline import (
    SCREEN_SOURCE_ID,
    CameraSample,
    MeetingTimeline,
)
from tests._perception_fakes import START, keyframe, slide_jpeg
from tests._perception_publication_fakes import Clock
from tests.test_camera_evidence import jpeg_of

ROADMAP = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])
ROADMAP_EDITED = slide_jpeg("Launch Timeline", ["Beta: 18 March 2027"])
BUDGET = slide_jpeg("Budget", ["Total: 4 million"], background=(90, 20, 20))


class Scene:
    def __init__(self) -> None:
        self.timeline = MeetingTimeline()
        self.clock = Clock()
        self.context = PerceptionContext(
            self.timeline, None, max_chars=6000, camera_monotonic=self.clock
        )
        self.revalidate = SceneRevalidator(self.context, now=lambda: 100.0)
        self.presenter = self.timeline.participant("device-ayu", "Ayu")
        self.show(ROADMAP, 0.0)

    def show(self, data: bytes, seconds: float) -> None:
        self.timeline.show_screen(keyframe(data, seconds=seconds), self.presenter, data=data)

    def screen_evidence(self) -> EvidenceBinding:
        segment = self.timeline.snapshot().current_screen
        assert segment is not None
        latest = self.timeline.latest_keyframe(segment)
        ref = SnapshotRef("screen", segment.ref, latest.sha256, 0, 0, 0, latest.captured_at)
        analyzed = self.timeline.analyzed_sample(ref)
        assert analyzed is not None
        return EvidenceBinding(
            "screen",
            "gather",
            ref,
            100.0,
            "fake/vision",
            "attempt",
            "inference",
            status="produced",
            analyzed=analyzed,
        )

    def camera(self, color: tuple[int, int, int], seconds: float, stream: str = "s") -> None:
        self.timeline.camera_frames.put(
            "device-budi",
            CameraSample(jpeg_of(color), stream, START + timedelta(seconds=seconds), 0, 100.0),
        )

    def camera_evidence(self) -> EvidenceBinding:
        frame = self.timeline.camera_frames.get("device-budi")
        assert frame is not None
        assert frame.content is not None
        return EvidenceBinding(
            "participant",
            "gather",
            frame.ref,
            100.0,
            "fake/vision",
            "attempt",
            "inference",
            status="produced",
            analyzed=frame.content,
        )

    def match(self, evidence: EvidenceBinding) -> MatchLevel | None:
        return self.revalidate.validate(evidence).match


def digest_only(scene: Scene, sequence: int, **changes: Any) -> None:
    latest = scene.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    record = replace(latest.record, sequence=sequence, **changes)
    scene.timeline.confirm(record, captured_at=START + timedelta(seconds=sequence), capture=None)


def test_no_sample_after_the_analyzed_frame_has_no_match_level() -> None:
    scene = Scene()

    assert scene.match(scene.screen_evidence()) is None


def test_later_sample_with_the_same_digest_is_exact() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    digest_only(scene, 2)

    validation = scene.revalidate.validate(evidence)
    assert validation.match == "exact"
    assert validation.confirmation is not None
    assert validation.confirmation.record.sequence == 2


def test_later_sample_of_the_same_segment_with_another_digest_is_similar() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    scene.show(ROADMAP_EDITED, 1.0)

    assert evidence.snapshot is not None
    assert scene.timeline.snapshot().current_ref == evidence.snapshot.subject_id
    assert scene.match(evidence) == "similar"


def test_digest_only_sample_with_another_digest_is_similar_without_any_frame() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    digest_only(scene, 2, digest="0" * 64)

    assert scene.match(evidence) == "similar"


def test_newer_non_exact_sample_outvotes_an_older_exact_one() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()
    digest_only(scene, 2)
    assert scene.match(evidence) == "exact"

    digest_only(scene, 3, digest="0" * 64)

    assert scene.match(evidence) == "similar"


def test_content_that_left_and_came_back_is_exact_with_a_broken_continuity() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()
    scene.show(BUDGET, 1.0)

    scene.show(ROADMAP, 2.0)

    validation = scene.revalidate.validate(evidence)
    assert validation.match == "exact"
    assert evidence.analyzed is not None
    assert validation.confirmation is not None
    assert validation.confirmation.continuity != evidence.analyzed.continuity


def test_stopped_share_is_reported_as_a_stopped_source() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    scene.timeline.stop_screen(START + timedelta(seconds=1))

    assert scene.revalidate.validate(evidence).source_stopped


def test_another_segment_is_distinct() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    scene.show(BUDGET, 1.0)

    assert scene.match(evidence) == "distinct"


@pytest.mark.parametrize(
    "change",
    [
        {"digest": None, "skip_reason": "hash_over_budget"},
        {"width": 640},
        {"pixel_format": "NV12"},
        {"algorithm": DECODED_PIXELS_SHA256},
        {"version": 2},
    ],
)
def test_later_sample_that_cannot_be_compared_is_unknown(change: dict[str, Any]) -> None:
    scene = Scene()
    evidence = scene.screen_evidence()

    digest_only(scene, 2, **change)

    assert scene.match(evidence) == "unknown"


def test_analyzed_frame_without_a_digest_is_unknown_once_any_sample_exists() -> None:
    scene = Scene()
    evidence = replace(scene.screen_evidence(), analyzed=None)

    assert scene.match(evidence) == "unknown"


def test_sample_with_an_inconsistent_capture_time_is_unknown() -> None:
    scene = Scene()
    scene.show(ROADMAP, 5.0)
    evidence = scene.screen_evidence()

    scene.timeline.show_screen(keyframe(ROADMAP, seconds=1.0), scene.presenter, data=ROADMAP)

    assert scene.match(evidence) == "unknown"


def test_repeated_frame_does_not_become_the_confirmation() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()
    assert evidence.analyzed is not None
    first = replace(evidence.analyzed.record, sequence=2, media_timestamp=1000.0)
    scene.timeline.confirm(first, captured_at=START + timedelta(seconds=1), capture=None)

    repeated = replace(first, sequence=3)
    scene.timeline.confirm(repeated, captured_at=START + timedelta(seconds=30), capture=None)

    validation = scene.revalidate.validate(evidence)
    assert validation.confirmation is not None
    assert validation.confirmation.record.sequence == 2
    assert validation.confirmation.captured_at == START + timedelta(seconds=1)


def test_camera_frame_with_another_digest_is_similar_never_distinct() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    evidence = scene.camera_evidence()

    scene.camera((30, 200, 30), 1.0)

    assert scene.match(evidence) == "similar"


def test_identical_camera_frame_is_exact() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    evidence = scene.camera_evidence()

    scene.camera((120, 120, 120), 1.0)

    assert scene.match(evidence) == "exact"


def test_camera_heartbeat_without_a_record_is_unknown() -> None:
    scene = Scene()
    scene.camera((120, 120, 120), 0.0)
    evidence = scene.camera_evidence()

    scene.timeline.camera_frames.unverified_heartbeat(
        "device-budi", captured_at=START + timedelta(seconds=4)
    )

    assert scene.match(evidence) == "unknown"


def test_analyzed_record_is_not_changed_by_later_samples() -> None:
    scene = Scene()
    evidence = scene.screen_evidence()
    before = (evidence.analyzed, evidence.snapshot)

    scene.show(ROADMAP_EDITED, 1.0)
    digest_only(scene, 7)
    scene.show(BUDGET, 9.0)

    assert (evidence.analyzed, evidence.snapshot) == before
    assert evidence.analyzed is not None
    assert isinstance(evidence.analyzed.record, DigestRecord)


def test_restarted_sampler_with_lower_sequence_is_still_a_later_confirmation() -> None:
    scene = Scene()
    digest_only(scene, 500, stream_id="share", generation=1, media_timestamp=500_000.0)
    item = scene.screen_evidence()
    assert item.analyzed is not None
    item = replace(item, analyzed=scene.timeline.samples.latest("screen", SCREEN_SOURCE_ID))
    latest = scene.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    scene.timeline.samples.stop("screen", SCREEN_SOURCE_ID)
    scene.timeline.confirm(
        replace(latest.record, generation=2, sequence=1, media_timestamp=1.0),
        captured_at=START + timedelta(seconds=501),
        capture=None,
    )

    validation = scene.revalidate.validate(item)

    assert validation.match == "exact"
    assert validation.source_changed is True
    assert validation.confirmation is not None
    assert item.analyzed is not None
    assert validation.confirmation.continuity != item.analyzed.continuity


def test_identical_later_frame_keeps_the_original_analyzed_sample() -> None:
    scene = Scene()
    item = scene.screen_evidence()
    original = item.analyzed
    assert item.snapshot is not None
    assert original is not None

    scene.show(ROADMAP, 2.0)

    assert scene.timeline.analyzed_sample(item.snapshot) is original
    assert original.captured_at == START
