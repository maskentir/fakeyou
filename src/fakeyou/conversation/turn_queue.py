import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace

from fakeyou.foundation.clock import monotonic
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.foundation.triggers import ReplyChannel

DEFERRED_RETRIES = 1


class PendingFinals:
    def __init__(self) -> None:
        self._events: deque[TranscriptEvent] = deque()

    @property
    def pending(self) -> bool:
        return bool(self._events)

    def admit(self, event: TranscriptEvent) -> None:
        self._events.append(event)

    def holds(self, event: TranscriptEvent) -> bool:
        return any(pending.id == event.id for pending in self._events)

    def take(self) -> tuple[TranscriptEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    def clear(self) -> None:
        self._events.clear()


class AdmittedFinals:
    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def advance(self) -> int:
        self._count += 1
        return self._count


class BargeLatch:
    def __init__(self) -> None:
        self.interrupted = False
        self.last_interim: str | None = None
        self.stream_stops = 0

    def rearm(self) -> None:
        self.interrupted = False
        self.last_interim = None


@dataclass(frozen=True, slots=True)
class DeferredTurn:
    event: TranscriptEvent
    channel: ReplyChannel
    expires_at: float
    needs_quiet_floor: bool
    attempts: int = 0


class DeferredTurns:
    def __init__(self) -> None:
        self._turns: deque[DeferredTurn] = deque()

    @property
    def pending(self) -> bool:
        return bool(self._turns)

    def holds(self, event_id: str) -> bool:
        return any(turn.event.id == event_id for turn in self._turns)

    def add(self, turn: DeferredTurn) -> bool:
        if self.holds(turn.event.id):
            return False
        self._turns.append(turn)
        return True

    def expire(self, now: float) -> list[DeferredTurn]:
        expired = [turn for turn in self._turns if turn.expires_at <= now]
        if expired:
            self._turns = deque(turn for turn in self._turns if turn.expires_at > now)
        return expired

    def next_ready(self, ready: Callable[[DeferredTurn], bool]) -> DeferredTurn | None:
        for turn in self._turns:
            if ready(turn):
                self._turns.remove(turn)
                return turn
        return None

    def next_ready_for(
        self, channel: ReplyChannel, ready: Callable[[DeferredTurn], bool]
    ) -> DeferredTurn | None:
        for turn in self._turns:
            if turn.channel == channel and ready(turn):
                self._turns.remove(turn)
                return turn
        return None

    def pending_for(self, channel: ReplyChannel) -> bool:
        return any(turn.channel == channel for turn in self._turns)

    def retry(self, turn: DeferredTurn) -> bool:
        if turn.attempts >= DEFERRED_RETRIES:
            return False
        self._turns.appendleft(replace(turn, attempts=turn.attempts + 1))
        return True

    def earliest_expiry(self) -> float | None:
        return min((turn.expires_at for turn in self._turns), default=None)

    def clear(self) -> None:
        self._turns.clear()


class QuietFloor:
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._last_activity = -math.inf

    def note_activity(self) -> None:
        self._last_activity = self._clock()

    def quiet_for(self) -> float:
        return self._clock() - self._last_activity
