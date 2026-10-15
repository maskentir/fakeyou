from typing import Any

from fakeyou.audio.voice_activity import VoiceActivityTracker
from tests._fakes_common import FakeClock


def batch(
    page_ms: float,
    levels: list[list[Any]],
    participants: list[dict[str, str]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    roster = participants if participants is not None else [{"id": "d1", "name": "Rani"}]
    return {"instance": "i1", "page_ms": page_ms, "levels": levels, "participants": roster, **extra}


def tracker_at(clock: FakeClock) -> VoiceActivityTracker:
    return VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic)


def speak(tracker: VoiceActivityTracker, clock: FakeClock, start: float, end: float) -> None:
    clock.now = start
    tracker.observe_vad(active=True)
    clock.now = end
    tracker.observe_vad(active=False)


def frames_batch(page_ms: float, frames: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {**batch(page_ms, [], **extra), "frames": frames}
