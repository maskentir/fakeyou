from collections import deque

from fakeyou.foundation.transcript import TranscriptEvent


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
