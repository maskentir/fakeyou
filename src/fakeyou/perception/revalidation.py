from collections.abc import Callable

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.content_digest import ContentSample, DigestRecord, MatchLevel
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
        return Validation(
            checked_at=checked_at,
            clock_domain=self._clock_domain,
            current_snapshot=current,
            supported=True if evidence.status == "produced" else None,
            unchanged=None if bound is None else same_scene,
            observed_at=scene.seen_at if isinstance(scene, CameraFrame) else checked_at,
            uncertainty_seconds=0.0,
            match=_match(evidence.digest, latest, same_scene=same_scene),
            confirmation=latest,
        )

    def _latest(self, bound: SnapshotRef) -> ContentSample | None:
        subject = SCREEN_SOURCE_ID if bound.source == "screen" else bound.subject_id
        return self._context.timeline.samples.latest(bound.source, subject)


def _same_source(bound: SnapshotRef, current: SnapshotRef) -> bool:
    if bound.source == "screen":
        return current.source == "screen" and bound.subject_id == current.subject_id
    return _same_scene(bound, current)


def _match(
    analyzed: DigestRecord | None, latest: ContentSample | None, *, same_scene: bool
) -> MatchLevel | None:
    if latest is None:
        return None
    if analyzed is None:
        return "unknown"
    if latest.record.sequence <= analyzed.sequence:
        return None
    same = analyzed.same_content_as(latest.record)
    if same is None or not latest.consistent:
        return "unknown"
    if same:
        return "exact"
    if analyzed.role == "camera":
        return "similar"
    return "similar" if same_scene else "distinct"
