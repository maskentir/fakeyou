from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from fakeyou.observability.journal import Journal
from fakeyou.perception.frames import compare_screens
from fakeyou.perception.percepts import (
    CHAT_REF_PREFIX,
    PERSON_REF_PREFIX,
    SCREEN_REF_PREFIX,
    CameraGlance,
    ChatLine,
    IncomingChat,
    Keyframe,
    MeetingSignal,
    ParticipantRef,
    ScreenReading,
    ScreenSegment,
    ScreenVisit,
    SignalKind,
)

KEYFRAME_CACHE_SIZE = 48
CAMERA_FRAME_CACHE_SIZE = 32
ScreenChange = Literal["new", "revisit", "same", "merged"]


@dataclass(frozen=True, slots=True)
class ScreenShown:
    segment: ScreenSegment
    change: ScreenChange


@dataclass(frozen=True, slots=True)
class ScreenRead:
    segment: ScreenSegment


@dataclass(frozen=True, slots=True)
class ChatReceived:
    line: ChatLine


@dataclass(frozen=True, slots=True)
class SignalObserved:
    signal: MeetingSignal


@dataclass(frozen=True, slots=True)
class CameraRead:
    glance: CameraGlance


TimelineEvent = ScreenShown | ScreenRead | ChatReceived | SignalObserved | CameraRead
TimelineSubscriber = Callable[[TimelineEvent], None]


@dataclass(frozen=True, slots=True)
class TimelineSnapshot:
    screens: tuple[ScreenSegment, ...] = ()
    current_ref: str | None = None
    chat: tuple[ChatLine, ...] = ()
    signals: tuple[MeetingSignal, ...] = ()
    participants: tuple[ParticipantRef, ...] = ()
    cameras: tuple[CameraGlance, ...] = ()
    presenting: ParticipantRef | None = None
    aliases: tuple[tuple[str, str], ...] = ()

    @property
    def current_screen(self) -> ScreenSegment | None:
        return self.screen(self.current_ref) if self.current_ref else None

    def screen(self, ref: str) -> ScreenSegment | None:
        target = dict(self.aliases).get(ref, ref)
        return next((screen for screen in self.screens if screen.ref == target), None)

    def screens_by_recency(self) -> list[ScreenSegment]:
        return sorted(self.screens, key=lambda screen: screen.last_seen, reverse=True)

    def previous_screen(self) -> ScreenSegment | None:
        earlier = [screen for screen in self.screens_by_recency() if screen.ref != self.current_ref]
        return earlier[0] if earlier else None


@dataclass(slots=True)
class _Registry:
    participants: dict[str, ParticipantRef] = field(default_factory=dict)
    screens: dict[str, ScreenSegment] = field(default_factory=dict)
    chat: dict[str, ChatLine] = field(default_factory=dict)
    signals: list[MeetingSignal] = field(default_factory=list)
    cameras: dict[str, CameraGlance] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    current_ref: str | None = None
    presenting: ParticipantRef | None = None
    screens_created: int = 0


def _reads_alike(screen: ScreenSegment, reading: ScreenReading) -> bool:
    return screen.reading is not None and screen.reading.matches(reading)


class KeyframeCache:
    def __init__(self, *, capacity: int = KEYFRAME_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._frames: OrderedDict[str, bytes] = OrderedDict()

    def put(self, sha256: str, data: bytes) -> None:
        self._frames[sha256] = data
        self._frames.move_to_end(sha256)
        while len(self._frames) > self._capacity:
            self._frames.popitem(last=False)

    def get(self, sha256: str) -> bytes | None:
        data = self._frames.get(sha256)
        if data is not None:
            self._frames.move_to_end(sha256)
        return data


class MeetingTimeline:
    def __init__(self, *, journal: Journal | None = None) -> None:
        self._journal = journal or Journal("", echo=False)
        self._state = _Registry()
        self._subscribers: list[TimelineSubscriber] = []
        self.keyframes = KeyframeCache()
        self.camera_frames = KeyframeCache(capacity=CAMERA_FRAME_CACHE_SIZE)

    def subscribe(self, subscriber: TimelineSubscriber) -> None:
        self._subscribers.append(subscriber)

    def unsubscribe(self, subscriber: TimelineSubscriber) -> None:
        if subscriber in self._subscribers:
            self._subscribers.remove(subscriber)

    def snapshot(self) -> TimelineSnapshot:
        state = self._state
        return TimelineSnapshot(
            screens=tuple(state.screens.values()),
            current_ref=state.current_ref,
            chat=tuple(state.chat.values()),
            signals=tuple(state.signals),
            participants=tuple(state.participants.values()),
            cameras=tuple(state.cameras.values()),
            presenting=state.presenting,
            aliases=tuple(state.aliases.items()),
        )

    def participant(self, participant_id: str, name: str) -> ParticipantRef:
        known = self._state.participants.get(participant_id)
        if known is not None and (known.name == name or not name):
            return known
        ref = known.ref if known else f"{PERSON_REF_PREFIX}{len(self._state.participants) + 1}"
        updated = ParticipantRef(ref=ref, participant_id=participant_id, name=name)
        self._state.participants[participant_id] = updated
        return updated

    def leave(self, participant_id: str) -> None:
        self._state.participants.pop(participant_id, None)

    def show_screen(
        self, keyframe: Keyframe, presenter: ParticipantRef | None, *, data: bytes | None = None
    ) -> ScreenShown:
        if data is not None:
            self.keyframes.put(keyframe.sha256, data)
        state = self._state
        current = state.screens.get(state.current_ref) if state.current_ref else None
        if (
            current is not None
            and current.showing
            and compare_screens(current.keyframe, keyframe) != "distinct"
        ):
            return ScreenShown(segment=current, change="same")
        self._close_current(keyframe.captured_at)
        visit = ScreenVisit(started_at=keyframe.captured_at)
        known = next(
            (
                screen
                for screen in state.screens.values()
                if compare_screens(screen.keyframe, keyframe) != "distinct"
            ),
            None,
        )
        if known is None:
            state.screens_created += 1
            segment = ScreenSegment(
                ref=f"{SCREEN_REF_PREFIX}{state.screens_created}",
                keyframe=keyframe,
                presenter=presenter,
                visits=(visit,),
            )
            shown = ScreenShown(segment=segment, change="new")
        else:
            segment = known.model_copy(update={"visits": (*known.visits, visit)})
            shown = ScreenShown(segment=segment, change="revisit")
        state.screens[segment.ref] = segment
        state.current_ref = segment.ref
        self._journal.record(
            "screen_captured",
            source="perception",
            ref=segment.ref,
            change=shown.change,
            width=keyframe.width,
            height=keyframe.height,
        )
        self._emit(shown)
        return shown

    def stop_screen(self, at: datetime) -> None:
        self._close_current(at)
        self._state.current_ref = None

    def read_screen(self, ref: str, reading: ScreenReading | None) -> ScreenSegment | None:
        state = self._state
        segment = state.screens.get(ref)
        if segment is None:
            return None
        if reading is None:
            segment = segment.model_copy(update={"reading_failed": True})
            state.screens[ref] = segment
            return segment
        twin = next(
            (
                screen
                for screen in state.screens.values()
                if screen.ref != ref
                and screen.reading is not None
                and screen.reading.matches(reading)
            ),
            None,
        )
        stored = reading.title_only()
        if twin is None:
            segment = segment.model_copy(update={"reading": stored})
            state.screens[ref] = segment
            self._emit(ScreenRead(segment=segment))
            return segment
        return self._merge(segment, into=twin, reading=stored)

    def _merge(
        self, segment: ScreenSegment, *, into: ScreenSegment, reading: ScreenReading
    ) -> ScreenSegment:
        state = self._state
        visits = tuple(sorted((*into.visits, *segment.visits), key=lambda visit: visit.started_at))
        merged = into.model_copy(
            update={"visits": visits, "reading": reading, "keyframe": segment.keyframe}
        )
        del state.screens[segment.ref]
        state.screens[into.ref] = merged
        state.aliases[segment.ref] = into.ref
        for alias, target in tuple(state.aliases.items()):
            if target == segment.ref:
                state.aliases[alias] = into.ref
        if state.current_ref == segment.ref:
            state.current_ref = into.ref
        self._journal.record(
            "screen_duplicate_detected", source="perception", ref=segment.ref, into=into.ref
        )
        self._emit(ScreenShown(segment=merged, change="merged"))
        self._emit(ScreenRead(segment=merged))
        return merged

    def add_chat(self, incoming: IncomingChat) -> ChatLine | None:
        if incoming.message_id in self._state.chat or not incoming.text.strip():
            return None
        line = ChatLine(
            ref=f"{CHAT_REF_PREFIX}{len(self._state.chat) + 1}",
            **incoming.model_dump(),
        )
        self._state.chat[incoming.message_id] = line
        self._journal.record(
            "chat_line_observed",
            source="perception",
            ref=line.ref,
            chars=len(line.text),
            own=line.own,
            backlog=line.backlog,
        )
        self._emit(ChatReceived(line=line))
        return line

    def add_signal(
        self,
        kind: SignalKind,
        participant: ParticipantRef,
        at: datetime,
        *,
        detail: str | None = None,
    ) -> MeetingSignal:
        signal = MeetingSignal(kind=kind, participant=participant, at=at, detail=detail)
        self._state.signals.append(signal)
        if kind == "presenting_started":
            self._state.presenting = participant
        elif kind == "presenting_stopped" and self._state.presenting == participant:
            self._state.presenting = None
            self.stop_screen(at)
        self._journal.record(
            "meeting_signal_observed", source="perception", kind=kind, participant=participant.ref
        )
        self._emit(SignalObserved(signal=signal))
        return signal

    def glance_camera(self, glance: CameraGlance) -> None:
        self._state.cameras[glance.participant.participant_id] = glance
        self._emit(CameraRead(glance=glance))

    def _close_current(self, at: datetime) -> None:
        state = self._state
        current = state.screens.get(state.current_ref) if state.current_ref else None
        if current is None or not current.showing:
            return
        closed = current.visits[-1].model_copy(update={"ended_at": at})
        state.screens[current.ref] = current.model_copy(
            update={"visits": (*current.visits[:-1], closed)}
        )

    def _emit(self, event: TimelineEvent) -> None:
        for subscriber in tuple(self._subscribers):
            subscriber(event)
