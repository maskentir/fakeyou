import hashlib
import json
from dataclasses import dataclass
from typing import Literal

CommandKind = Literal["raise_hand", "lower_hand", "react", "mute", "unmute", "send_chat", "leave"]
CommandState = Literal[
    "accepted",
    "ready",
    "dispatching",
    "effect_observed",
    "effect_unobserved",
    "unknown",
    "failed_after_boundary",
    "refused",
    "cancelled_before_boundary",
    "failed_before_boundary",
]
Verification = Literal["readback", "dom_echo", "acted_only"]
TOGGLES: frozenset[CommandKind] = frozenset({"raise_hand", "lower_hand", "mute", "unmute"})
MAX_TOGGLE_ATTEMPTS = 3
TERMINAL_COMMAND_STATES: frozenset[CommandState] = frozenset(
    {
        "effect_observed",
        "effect_unobserved",
        "unknown",
        "failed_after_boundary",
        "refused",
        "cancelled_before_boundary",
        "failed_before_boundary",
    }
)
_BEFORE_BOUNDARY_EXITS: frozenset[CommandState] = frozenset(
    {"refused", "cancelled_before_boundary", "failed_before_boundary"}
)
_AFTER_BOUNDARY_EXITS: frozenset[CommandState] = frozenset(
    {"effect_observed", "effect_unobserved", "unknown", "failed_after_boundary"}
)


class CommandTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class CommandSpec:
    kind: CommandKind
    text: str = ""

    @property
    def verification(self) -> Verification:
        if self.kind in TOGGLES:
            return "readback"
        if self.kind == "send_chat":
            return "dom_echo"
        return "acted_only"


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    command_id: str
    kind: CommandKind
    state: CommandState
    detail: str = ""

    @property
    def observed(self) -> bool:
        return self.state == "effect_observed"

    @property
    def not_done(self) -> bool:
        return self.state in _BEFORE_BOUNDARY_EXITS | {"failed_after_boundary"}


def command_id(attempt_id: str, plan_id: str, ordinal: int, spec: CommandSpec) -> str:
    material = json.dumps([attempt_id, plan_id, ordinal, spec.kind, spec.text], ensure_ascii=False)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def transition(kind: CommandKind, state: CommandState, to: CommandState) -> CommandState:
    if state in TERMINAL_COMMAND_STATES:
        raise CommandTransitionError(f"command already {state}")
    allowed: frozenset[CommandState]
    match state:
        case "accepted":
            allowed = _BEFORE_BOUNDARY_EXITS | {"ready"}
        case "ready":
            allowed = _BEFORE_BOUNDARY_EXITS | {"dispatching", "effect_observed"}
        case "dispatching":
            allowed = _AFTER_BOUNDARY_EXITS | ({"dispatching"} if kind in TOGGLES else frozenset())
        case _:
            allowed = frozenset()
    if to not in allowed:
        raise CommandTransitionError(f"{kind} cannot move from {state} to {to}")
    return to


def recovered(state: CommandState) -> CommandState:
    if state in TERMINAL_COMMAND_STATES:
        return state
    if state == "dispatching":
        return "unknown"
    return "cancelled_before_boundary"


def visible_effect_possible(state: CommandState) -> bool:
    return state in {"dispatching", *_AFTER_BOUNDARY_EXITS} - {"failed_after_boundary"}
