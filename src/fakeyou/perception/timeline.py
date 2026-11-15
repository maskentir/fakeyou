import hashlib
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from fakeyou.foundation.clock_mapping import MappedInstant
from fakeyou.foundation.content_digest import (
    HEARTBEAT_WITHOUT_RECORD,
    ContentSample,
    DigestRecord,
    SampleSource,
    encoded_bytes_record,
)
from fakeyou.observability.journal import Journal
from fakeyou.perception.evidence import SnapshotRef
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
from fakeyou.perception.samples import IngestOutcome, SampleLedger

KEYFRAME_CACHE_SIZE = 48
CAMERA_FRAME_CACHE_SIZE = 32
ANALYZED_SAMPLE_CACHE_SIZE = 256
SCREEN_SOURCE_ID = "screen"
CaptureClock = Callable[[datetime], MappedInstant | None]
ScreenChange = Literal["new", "revisit", "same", "merged"]


@dataclass(frozen=True, slots=True)
class ScreenShown:
    segment: ScreenSegment
    change: ScreenChange
    arrival: Keyframe | None = None


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


@dataclass(frozen=True, slots=True)
class FrameSample:
    record: DigestRecord
    capture: MappedInstant | None


@dataclass(frozen=True, slots=True)
class CameraSample:
    data: bytes
    stream_id: str
    captured_at: datetime
    epoch: int
    seen_at: float
    sample: FrameSample | None = None


@dataclass(frozen=True, slots=True)
class CameraFrame:
    data: bytes
    seen_at: float
    ref: SnapshotRef
    content: ContentSample | None = None


class CameraFrameStore:
    def __init__(self, *, capacity: int = CAMERA_FRAME_CACHE_SIZE) -> None:
        self._capacity = capacity
        self._frames: OrderedDict[str, CameraFrame] = OrderedDict()
        self._sequences: dict[str, int] = {}
        self._generations: dict[str, int] = {}
        self._epoch = 0
        self._invalidator: Callable[[str | None], None] | None = None
        self.samples = SampleLedger()
        self.capture_clock: CaptureClock | None = None

    def set_invalidator(self, invalidator: Callable[[str | None], None]) -> None:
        self._invalidator = invalidator

    def put(self, participant_id: str, sample: CameraSample) -> CameraFrame:
        sequence = self._sequences.get(participant_id, 0) + 1
        self._sequences[participant_id] = sequence
        stored = CameraFrame(
            data=sample.data,
            seen_at=sample.seen_at,
            content=self._ingest(participant_id, sample),
            ref=SnapshotRef(
                source="camera",
                subject_id=participant_id,
                stream_id=sample.stream_id,
                epoch=sample.epoch,
                sequence=sequence,
                generation=self._generations.get(participant_id, 0),
                captured_at=sample.captured_at,
            ),
        )
        self._frames[participant_id] = stored
        self._frames.move_to_end(participant_id)
        while len(self._frames) > self._capacity:
            self._frames.popitem(last=False)
        return stored

    def _ingest(self, participant_id: str, sample: CameraSample) -> ContentSample:
        given = sample.sample
        record = (
            given.record
            if given is not None
            else encoded_bytes_record(
                hashlib.sha256(sample.data).hexdigest(),
                SampleSource("camera", sample.stream_id, participant_id),
                sequence=self.samples.next_sequence("camera", participant_id),
                size=(0, 0),
            )
        )
        clock = self.capture_clock
        capture = (
            given.capture
            if given is not None
            else (clock(sample.captured_at) if clock is not None else None)
        )
        self.samples.ingest(record, captured_at=sample.captured_at, capture=capture)
        return ContentSample(record, sample.captured_at, capture, self.samples.revision)

    def unverified_heartbeat(self, participant_id: str, *, captured_at: datetime) -> None:
        frame = self._frames.get(participant_id)
        if frame is None:
            return
        record = DigestRecord(
            algorithm="",
            version=0,
            width=0,
            height=0,
            pixel_format="",
            role="camera",
            stream_id=frame.ref.stream_id,
            subject_id=participant_id,
            sequence=self.samples.next_sequence("camera", participant_id),
            media_timestamp=None,
            digest=None,
            skip_reason=HEARTBEAT_WITHOUT_RECORD,
        )
        self.samples.ingest(record, captured_at=captured_at, capture=None)

    def get(self, participant_id: str) -> CameraFrame | None:
        frame = self._frames.get(participant_id)
        if frame is not None:
            self._frames.move_to_end(participant_id)
        return frame

    def touch(self, participant_id: str, *, seen_at: float) -> None:
        frame = self._frames.get(participant_id)
        if frame is None:
            return
        self._frames[participant_id] = replace(frame, seen_at=seen_at)
        self._frames.move_to_end(participant_id)

    def drop(self, participant_id: str) -> None:
        self._generations[participant_id] = self._generations.get(participant_id, 0) + 1
        self._frames.pop(participant_id, None)
        self.samples.stop("camera", participant_id)
        if self._invalidator is not None:
            self._invalidator(participant_id)

    def clear(self) -> None:
        self._epoch += 1
        for participant_id in self._frames:
            self.samples.stop("camera", participant_id)
        self._frames.clear()
        if self._invalidator is not None:
            self._invalidator(None)

    def current_generation(self, participant_id: str) -> int:
        return self._generations.get(participant_id, 0)

    def current_epoch(self) -> int:
        return self._epoch


class MeetingTimeline:
    def __init__(self, *, journal: Journal | None = None) -> None:
        self._journal = journal or Journal("", echo=False)
        self._state = _Registry()
        self._subscribers: list[TimelineSubscriber] = []
        self.keyframes = KeyframeCache()
        self.camera_frames = CameraFrameStore()
        self.samples = self.camera_frames.samples
        self._latest_keyframes: dict[str, Keyframe] = {}
        self._analyzed: OrderedDict[str, ContentSample] = OrderedDict()

    @property
    def capture_clock(self) -> CaptureClock | None:
        return self.camera_frames.capture_clock

    @capture_clock.setter
    def capture_clock(self, clock: CaptureClock | None) -> None:
        self.camera_frames.capture_clock = clock

    def latest_keyframe(self, segment: ScreenSegment) -> Keyframe:
        return self._latest_keyframes.get(segment.ref, segment.keyframe)

    def analyzed_sample(self, ref: SnapshotRef) -> ContentSample | None:
        if ref.source == "screen":
            return self._analyzed.get(ref.stream_id)
        frame = self.camera_frames.get(ref.subject_id)
        if frame is None or frame.ref != ref:
            return None
        return frame.content

    def confirm(self, sample: FrameSample, *, captured_at: datetime) -> IngestOutcome:
        return self.samples.ingest(sample.record, captured_at=captured_at, capture=sample.capture)

    def _ingest_screen(self, keyframe: Keyframe, sample: FrameSample | None) -> None:
        record = (
            sample.record
            if sample is not None
            else encoded_bytes_record(
                keyframe.sha256,
                SampleSource("screen", "", SCREEN_SOURCE_ID),
                sequence=self.samples.next_sequence("screen", SCREEN_SOURCE_ID),
                size=(keyframe.width, keyframe.height),
            )
        )
        clock = self.capture_clock
        capture = (
            sample.capture
            if sample is not None
            else (clock(keyframe.captured_at) if clock is not None else None)
        )
        self.samples.ingest(record, captured_at=keyframe.captured_at, capture=capture)
        self._analyzed[keyframe.sha256] = ContentSample(
            record, keyframe.captured_at, capture, self.samples.revision
        )
        self._analyzed.move_to_end(keyframe.sha256)
        while len(self._analyzed) > ANALYZED_SAMPLE_CACHE_SIZE:
            self._analyzed.popitem(last=False)

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
        self,
        keyframe: Keyframe,
        presenter: ParticipantRef | None,
        *,
        data: bytes | None = None,
        sample: FrameSample | None = None,
    ) -> ScreenShown:
        if data is not None:
            self.keyframes.put(keyframe.sha256, data)
        self._ingest_screen(keyframe, sample)
        state = self._state
        current = state.screens.get(state.current_ref) if state.current_ref else None
        if (
            current is not None
            and current.showing
            and compare_screens(current.keyframe, keyframe) != "distinct"
        ):
            self._latest_keyframes[current.ref] = keyframe
            shown = ScreenShown(segment=current, change="same", arrival=keyframe)
            self._emit(shown)
            return shown
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
            shown = ScreenShown(segment=segment, change="new", arrival=keyframe)
        else:
            segment = known.model_copy(update={"visits": (*known.visits, visit)})
            shown = ScreenShown(segment=segment, change="revisit", arrival=keyframe)
        state.screens[segment.ref] = segment
        state.current_ref = segment.ref
        self._latest_keyframes[segment.ref] = keyframe
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
        self.samples.stop("screen", SCREEN_SOURCE_ID)

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
        self._latest_keyframes[into.ref] = self._latest_keyframes.pop(segment.ref, segment.keyframe)
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
