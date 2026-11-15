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
    FrameSample,
    MeetingTimeline,
)
from tests._perception_fakes import START, keyframe, slide_jpeg
from tests.test_camera_evidence import jpeg_of

ROADMAP = slide_jpeg("Launch Timeline", ["Beta: 17 March 2027"])
ROADMAP_EDITED = slide_jpeg("Launch Timeline", ["Beta: 18 March 2027"])
BUDGET = slide_jpeg("Budget", ["Total: 4 million"], background=(90, 20, 20))


class Scene:
    def __init__(self) -> None:
        self.timeline = MeetingTimeline()
        self.context = PerceptionContext(
            self.timeline, None, max_chars=6000, camera_monotonic=lambda: 100.0
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
            digest=analyzed.record,
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
            digest=frame.content.record,
        )

    def match(self, evidence: EvidenceBinding) -> MatchLevel | None:
        return self.revalidate.validate(evidence).match


def digest_only(scene: Scene, sequence: int, **changes: Any) -> None:
    latest = scene.timeline.samples.latest("screen", SCREEN_SOURCE_ID)
    assert latest is not None
    record = replace(latest.record, sequence=sequence, **changes)
    scene.timeline.confirm(
        FrameSample(record, None), captured_at=START + timedelta(seconds=sequence)
    )


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
    evidence = replace(scene.screen_evidence(), digest=None)

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
    assert evidence.digest is not None
    first = replace(evidence.digest, sequence=2, media_timestamp=1000.0)
    scene.timeline.confirm(FrameSample(first, None), captured_at=START + timedelta(seconds=1))

    repeated = replace(first, sequence=3)
    scene.timeline.confirm(FrameSample(repeated, None), captured_at=START + timedelta(seconds=30))

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
    before = (evidence.digest, evidence.snapshot)

    scene.show(ROADMAP_EDITED, 1.0)
    digest_only(scene, 7)
    scene.show(BUDGET, 9.0)

    assert (evidence.digest, evidence.snapshot) == before
    assert isinstance(evidence.digest, DigestRecord)
