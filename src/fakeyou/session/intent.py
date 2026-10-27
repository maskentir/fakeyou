from dataclasses import dataclass
from typing import Literal, cast, get_args

from fakeyou.session.facts import StateQuestion

MeetActionKind = Literal["raise_hand", "lower_hand", "react", "mute", "unmute", "leave"]
EvidenceNeed = Literal["screen", "participant", "chat_history", "memory"]
Decision = Literal["speak", "silent", "clarify"]
MAX_EVIDENCE = 2
_ACTIONS: frozenset[str] = frozenset(get_args(MeetActionKind))
_EVIDENCE: tuple[EvidenceNeed, ...] = get_args(EvidenceNeed)
_STATES: frozenset[str] = frozenset(get_args(StateQuestion))


@dataclass(frozen=True, slots=True)
class NoResponse:
    reason: str


@dataclass(frozen=True, slots=True)
class Answer:
    evidence: tuple[EvidenceNeed, ...] = ()
    state: StateQuestion | None = None


@dataclass(frozen=True, slots=True)
class Clarify:
    pass


@dataclass(frozen=True, slots=True)
class ChatRequest:
    evidence: tuple[EvidenceNeed, ...] = ()


@dataclass(frozen=True, slots=True)
class MeetAction:
    kind: MeetActionKind
    emoji: str | None = None


@dataclass(frozen=True, slots=True)
class Compound:
    action: MeetAction
    output: Answer | ChatRequest


Output = Answer | ChatRequest
Intent = NoResponse | Answer | Clarify | ChatRequest | MeetAction | Compound


@dataclass(frozen=True, slots=True)
class Reading:
    decision: Decision
    reason: str
    meet_action: str = "none"
    reaction_emoji: str | None = None
    action_only: bool = False
    requests_chat: bool = False
    evidence: tuple[str, ...] = ()
    state_question: str = "none"
    language: str | None = None


def _state(value: str) -> StateQuestion | None:
    return cast(StateQuestion, value) if value in _STATES else None


def _evidence(needs: tuple[str, ...]) -> tuple[EvidenceNeed, ...]:
    wanted = dict.fromkeys(need for need in needs if need in _EVIDENCE)
    return tuple(need for need in _EVIDENCE if need in wanted)[:MAX_EVIDENCE]


def _action(reading: Reading) -> MeetAction | None:
    if reading.meet_action not in _ACTIONS:
        return None
    kind = cast(MeetActionKind, reading.meet_action)
    if kind == "react" and not reading.reaction_emoji:
        return None
    return MeetAction(kind, reading.reaction_emoji if kind == "react" else None)


def intent_of(reading: Reading) -> Intent:
    if reading.decision == "silent":
        return NoResponse(reading.reason)
    if reading.decision == "clarify":
        return Clarify()
    evidence = _evidence(reading.evidence)
    state = _state(reading.state_question)
    output: Output = (
        ChatRequest(evidence) if reading.requests_chat else Answer(() if state else evidence, state)
    )
    action = _action(reading)
    if action is None:
        return output
    if reading.action_only and not reading.requests_chat:
        return action
    return Compound(action, output)
