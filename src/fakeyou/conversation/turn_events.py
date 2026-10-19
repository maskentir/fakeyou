from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypedDict

from fakeyou.conversation.participation_decider import DeciderTurn
from fakeyou.foundation.participation import ParticipationContext
from fakeyou.foundation.taxonomy import Subsystem

TurnEventKind = Literal["stop", "final", "interim", "vad"]

_PRIORITY: dict[TurnEventKind, int] = {"stop": 3, "final": 2, "interim": 1, "vad": 1}


@dataclass(frozen=True, slots=True)
class TurnEvent:
    kind: TurnEventKind
    event_id: str
    at: float
    payload: str


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    winner: TurnEvent | None
    journaled: tuple[TurnEvent, ...]


class TurnState(TypedDict):
    last_event_id: str | None
    kind: TurnEventKind | None
    turns: list[DeciderTurn]
    context: ParticipationContext | None


class JournalSink(Protocol):
    def record(self, event: str, *, source: Subsystem, **fields: Any) -> object: ...


def _rank(event: TurnEvent) -> tuple[int, float, str]:
    return (_PRIORITY[event.kind], event.at, event.event_id)


def arbitrate(pending: Sequence[TurnEvent]) -> ArbitrationResult:
    if not pending:
        return ArbitrationResult(winner=None, journaled=())
    winner = max(pending, key=_rank)
    journaled = tuple(event for event in pending if event is not winner)
    return ArbitrationResult(winner=winner, journaled=journaled)


def apply_arbitration(
    state: TurnState, result: ArbitrationResult, journal: JournalSink
) -> TurnState:
    winner = result.winner
    if winner is None:
        return state
    for lost in result.journaled:
        journal.record(
            "turn_event_dropped",
            source="agent",
            event_id=lost.event_id,
            kind=lost.kind,
            winner_event_id=winner.event_id,
        )
    return TurnState(
        last_event_id=winner.event_id,
        kind=winner.kind,
        turns=state["turns"],
        context=state["context"],
    )
