import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import replace

from fakeyou.session.contracts import (
    OwnerControlObserved,
    ParticipantCountObserved,
    SessionEvent,
    Source,
    TranscriptObserved,
)
from fakeyou.session.world import DEFAULT_LIMITS, Limits, SessionState, reduce

DEFAULT_MAILBOX_CAPACITY = 512
DEFAULT_SEEN_EVENTS = 4096

StateListener = Callable[[SessionState, SessionEvent], None]


def is_control(event: SessionEvent) -> bool:
    return isinstance(event.observation, OwnerControlObserved)


def is_ephemeral(event: SessionEvent) -> bool:
    observation = event.observation
    if isinstance(observation, TranscriptObserved):
        return not observation.final
    return isinstance(observation, ParticipantCountObserved)


class Mailbox:
    def __init__(self, capacity: int = DEFAULT_MAILBOX_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._control: deque[SessionEvent] = deque()
        self._events: deque[SessionEvent] = deque()
        self._ready = asyncio.Event()
        self.dropped = 0

    def __len__(self) -> int:
        return len(self._control) + len(self._events)

    @property
    def saturated(self) -> bool:
        return len(self._events) > self._capacity

    def offer(self, event: SessionEvent) -> None:
        if is_control(event):
            self._control.append(event)
        else:
            if len(self._events) >= self._capacity:
                self._drop_ephemeral(incoming=event)
            if len(self._events) < self._capacity or not is_ephemeral(event):
                self._events.append(event)
        self._ready.set()

    def _drop_ephemeral(self, *, incoming: SessionEvent) -> None:
        for index, queued in enumerate(self._events):
            if is_ephemeral(queued):
                del self._events[index]
                self.dropped += 1
                return
        if is_ephemeral(incoming):
            self.dropped += 1

    def take_nowait(self) -> SessionEvent | None:
        if self._control:
            return self._control.popleft()
        if self._events:
            return self._events.popleft()
        self._ready.clear()
        return None

    async def take(self) -> SessionEvent:
        while (event := self.take_nowait()) is None:
            await self._ready.wait()
        return event


class SessionKernel:
    def __init__(
        self,
        *,
        session_id: str,
        attempt_id: str,
        limits: Limits = DEFAULT_LIMITS,
        seen_events: int = DEFAULT_SEEN_EVENTS,
    ) -> None:
        if seen_events < 1:
            raise ValueError("seen_events must be at least 1")
        self._session_id = session_id
        self._attempt_id = attempt_id
        self._limits = limits
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_limit = seen_events
        self._source_seq: dict[Source, int] = {}
        self._listeners: list[StateListener] = []
        self._state = SessionState()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def attempt_id(self) -> str:
        return self._attempt_id

    @property
    def state(self) -> SessionState:
        return self._state

    def subscribe(self, listener: StateListener) -> None:
        self._listeners.append(listener)

    def accept(self, event: SessionEvent) -> SessionState:
        if (event.session_id, event.attempt_id) != (self._session_id, self._attempt_id):
            raise ValueError("event belongs to another session attempt")
        if event.event_id in self._seen:
            return self._state
        self._remember(event.event_id)
        self._state = reduce(self._state, event, late=self._late(event), limits=self._limits)
        for listener in self._listeners:
            listener(self._state, event)
        return self._state

    def note_mailbox(self, mailbox: Mailbox) -> None:
        health = self._state.health
        if (health.dropped_events, health.saturated) == (mailbox.dropped, mailbox.saturated):
            return
        updated = replace(health, dropped_events=mailbox.dropped, saturated=mailbox.saturated)
        revisions = replace(self._state.revisions, health=self._state.revisions.health + 1)
        self._state = replace(self._state, health=updated, revisions=revisions)

    async def run(self, mailbox: Mailbox) -> None:
        while True:
            event = await mailbox.take()
            self.note_mailbox(mailbox)
            self.accept(event)

    def _remember(self, event_id: str) -> None:
        self._seen[event_id] = None
        if len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

    def _late(self, event: SessionEvent) -> bool:
        if event.source_seq is None:
            return False
        last = self._source_seq.get(event.source)
        if last is not None and event.source_seq <= last:
            return True
        self._source_seq[event.source] = event.source_seq
        return False
