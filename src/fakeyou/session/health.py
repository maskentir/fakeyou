from typing import Literal

from fakeyou.session.contracts import Source
from fakeyou.session.world import SessionState

Mode = Literal[
    "browser_unknown",
    "perception_degraded",
    "identity_degraded",
    "model_degraded",
    "voice_degraded",
    "memory_degraded",
    "backlog_saturated",
]
UNHEALTHY = frozenset({"stale", "down"})
_SOURCE_MODES: tuple[tuple[Source, Mode], ...] = (
    ("meet_protocol", "perception_degraded"),
    ("speaker_registry", "identity_degraded"),
    ("model", "model_degraded"),
    ("playback", "voice_degraded"),
    ("memory", "memory_degraded"),
)


def modes(state: SessionState) -> frozenset[Mode]:
    sources = state.health.sources
    degraded: set[Mode] = {
        mode for source, mode in _SOURCE_MODES if sources.get(source) in UNHEALTHY
    }
    if state.world.call_state != "in_call":
        degraded.add("browser_unknown")
    if state.health.saturated:
        degraded.add("backlog_saturated")
    return frozenset(degraded)


def side_effects_allowed(active: frozenset[Mode]) -> bool:
    return not active & {"browser_unknown", "model_degraded", "backlog_saturated"}
