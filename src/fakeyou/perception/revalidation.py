from collections.abc import Callable

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.clock_mapping import LOCAL_MONOTONIC
from fakeyou.foundation.publication import EvidenceBinding, SnapshotRef, Validation
from fakeyou.perception.context import PerceptionContext
from fakeyou.perception.timeline import CameraFrame


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
        checked_at = self._now()
        bound = evidence.snapshot
        scene = None if bound is None else self._context.current_scene(bound)
        current = scene.ref if isinstance(scene, CameraFrame) else scene
        return Validation(
            checked_at=checked_at,
            clock_domain=self._clock_domain,
            current_snapshot=current,
            supported=True if evidence.status == "produced" else None,
            unchanged=None
            if bound is None
            else current is not None and _same_scene(bound, current),
            observed_at=scene.seen_at if isinstance(scene, CameraFrame) else checked_at,
            uncertainty_seconds=0.0,
        )
