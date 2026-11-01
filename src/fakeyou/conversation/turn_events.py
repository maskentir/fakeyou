from dataclasses import dataclass
from typing import Any, Literal, Protocol

from fakeyou.foundation.taxonomy import Subsystem

TurnEventKind = Literal["stop", "final", "interim", "vad"]


@dataclass(frozen=True, slots=True)
class TurnEvent:
    kind: TurnEventKind
    event_id: str
    at: float
    payload: str


class JournalSink(Protocol):
    def record(self, event: str, *, source: Subsystem, **fields: Any) -> object: ...
