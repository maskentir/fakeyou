from collections.abc import Sequence
from typing import Any

from fakeyou.audio.voice_activity import MEET_AUDIO_SLOTS, VoiceActivityTracker
from fakeyou.identity.identity import SpeakerRegistry
from tests._fakes_common import FakeClock


def batch(page_ms: float, levels: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {"instance": "i1", "page_ms": page_ms, "levels": levels, **extra}


def tracker_at(clock: FakeClock, people: Sequence[str] = ("d1",)) -> VoiceActivityTracker:
    registry = SpeakerRegistry(capacity=MEET_AUDIO_SLOTS)
    registry.update_roster(people)
    return VoiceActivityTracker(monotonic=clock.monotonic, registry=registry)


def speak(tracker: VoiceActivityTracker, clock: FakeClock, start: float, end: float) -> None:
    clock.now = start
    tracker.observe_vad(active=True)
    clock.now = end
    tracker.observe_vad(active=False)


def frames_batch(page_ms: float, frames: list[list[Any]], **extra: Any) -> dict[str, Any]:
    return {**batch(page_ms, [], **extra), "frames": frames}
