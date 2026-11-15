from collections.abc import Callable

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.content_digest import ContentSample, MatchLevel
from fakeyou.foundation.publication import EvidenceBinding, SnapshotRef, Validation
from fakeyou.perception.context import PerceptionContext
from fakeyou.perception.timeline import SCREEN_SOURCE_ID, CameraFrame


def _same_scene(bound: SnapshotRef, current: SnapshotRef) -> bool:
    return (
        bound.source,
        bound.subject_id,
        bound.stream_id,
        bound.epoch,
        bound.generation,
    ) == (
        current.source,
        current.subject_id,
        current.stream_id,
        current.epoch,
        current.generation,
    )


class SceneRevalidator:
    def __init__(
        self,
        context: PerceptionContext,
        *,
        now: Callable[[], float] = monotonic,
        clock_domain: str = LOCAL_MONOTONIC,
    ) -> None:
        self._context = context
        self._now = now
        self._clock_domain = clock_domain

    async def __call__(self, evidence: EvidenceBinding) -> Validation:
        return self.validate(evidence)

    def validate(self, evidence: EvidenceBinding) -> Validation:
        checked_at = self._now()
        bound = evidence.snapshot
        scene = None if bound is None else self._context.current_scene(bound)
        current = scene.ref if isinstance(scene, CameraFrame) else scene
        same_scene = bound is not None and current is not None and _same_source(bound, current)
        latest = None if bound is None else self._latest(bound)
        analyzed = evidence.analyzed
        if (
            bound is not None
            and bound.source == "camera"
            and analyzed is not None
            and latest is not None
        ):
            same_scene = (
                same_scene and analyzed.record.sampler_identity == latest.record.sampler_identity
            )
        return Validation(
            checked_at=checked_at,
            clock_domain=self._clock_domain,
            current_snapshot=current,
            supported=True if evidence.status == "produced" else None,
            unchanged=None if bound is None else same_scene,
            observed_at=scene.seen_at if isinstance(scene, CameraFrame) else checked_at,
            uncertainty_seconds=0.0,
            match=_match(evidence.analyzed, latest, same_scene=same_scene),
            confirmation=latest,
            source_stopped=bound is not None and self._stopped(bound),
            source_changed=bound is not None
            and bound.source == "screen"
            and self._source_changed(bound, evidence.analyzed, latest, same_scene=same_scene),
            subject_changed=bound is not None
            and bound.source == "camera"
            and self._subject_changed(bound, evidence.analyzed),
        )

    def _stopped(self, bound: SnapshotRef) -> bool:
        timeline = self._context.timeline
        if bound.source == "screen":
            return (
                timeline.samples.stopped("screen", SCREEN_SOURCE_ID)
                or timeline.snapshot().current_screen is None
            )
        return timeline.samples.stopped("camera", bound.subject_id) or self._context.camera_gone(
            bound.subject_id
        )

    def _source_changed(
        self,
        bound: SnapshotRef,
        analyzed: ContentSample | None,
        latest: ContentSample | None,
        *,
        same_scene: bool,
    ) -> bool:
        if not same_scene:
            return True
        streams = (
            None if analyzed is None else analyzed.record.sampler_identity,
            None if latest is None else latest.record.sampler_identity,
        )
        if streams[0] and streams[1] and streams[0] != streams[1]:
            return True
        timeline = self._context.timeline
        segment = timeline.snapshot().screen(bound.subject_id)
        if segment is None:
            return True
        first, latest_presenter = segment.presenter, timeline.latest_presenter(segment)
        return (
            first is not None
            and latest_presenter is not None
            and first.participant_id != latest_presenter.participant_id
        )

    def _subject_changed(self, bound: SnapshotRef, analyzed: ContentSample | None) -> bool:
        stream_id = bound.stream_id if analyzed is None else analyzed.record.stream_id
        page = (
            None if analyzed is None else (analyzed.record.page_epoch, analyzed.record.page_session)
        )
        owner = self._context.timeline.samples.subject_of_stream("camera", stream_id, page=page)
        return owner is not None and owner != bound.subject_id

    def _latest(self, bound: SnapshotRef) -> ContentSample | None:
        subject = SCREEN_SOURCE_ID if bound.source == "screen" else bound.subject_id
        return self._context.timeline.samples.latest(bound.source, subject)


def _same_source(bound: SnapshotRef, current: SnapshotRef) -> bool:
    if bound.source == "screen":
        return current.source == "screen" and bound.subject_id == current.subject_id
    return _same_scene(bound, current)


def _match(
    analyzed: ContentSample | None, latest: ContentSample | None, *, same_scene: bool
) -> MatchLevel | None:
    if latest is None:
        return None
    if analyzed is None:
        return "unknown"
    record = analyzed.record
    if (
        latest.record.sampler_identity == record.sampler_identity
        and latest.record.sequence <= record.sequence
    ):
        return None
    same = record.same_content_as(latest.record)
    if same is None or not latest.consistent:
        return "unknown"
    if same:
        return "exact"
    if record.role == "camera":
        return "similar"
    return "similar" if same_scene else "distinct"
