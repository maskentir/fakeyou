from typing import TypedDict

from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase


class ExternalEffect(TypedDict):
    effect: str


class LifecycleState(TypedDict):
    owner_id: str
    twin_id: str
    meeting_id: str
    phase: PublicPhase
    terminal_reason: str | None
    external_effects: list[ExternalEffect]


def validate_phase_reason(phase: PublicPhase, terminal_reason: str | None) -> None:
    if phase in TERMINAL_PHASES and not terminal_reason:
        raise ValueError(f"phase {phase!r} requires a terminal_reason")
