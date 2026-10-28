from dataclasses import dataclass
from typing import TypedDict

from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase


class ExternalEffect(TypedDict):
    effect: str


class LifecycleState(TypedDict):
    owner_id: str
    twin_id: str
    meeting_id: str
    thread_id: str
    phase: PublicPhase
    terminal_reason: str | None
    external_effects: list[ExternalEffect]


LIFECYCLE_STATE_KEYS = frozenset(LifecycleState.__annotations__)


class MissingRuntimeHandle(LookupError):
    def __init__(self, name: str) -> None:
        super().__init__(f"missing runtime handle: {name}")
        self.name = name


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    browser: object | None = None
    audio: object | None = None
    stt: object | None = None
    graphiti: object | None = None
    session: object | None = None

    def require(self, name: str) -> object:
        handles = {
            "browser": self.browser,
            "audio": self.audio,
            "stt": self.stt,
            "graphiti": self.graphiti,
            "session": self.session,
        }
        handle = handles.get(name)
        if handle is None:
            raise MissingRuntimeHandle(name)
        return handle


def assert_checkpointable(state: LifecycleState) -> None:
    for key in state:
        if key not in LIFECYCLE_STATE_KEYS:
            raise ValueError(f"runtime value {key!r} must not enter checkpoint state")


def validate_phase_reason(phase: PublicPhase, terminal_reason: str | None) -> None:
    if phase in TERMINAL_PHASES and not terminal_reason:
        raise ValueError(f"phase {phase!r} requires a terminal_reason")
