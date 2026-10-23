import asyncio
import base64
import binascii
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, override

from pydantic import BaseModel, ConfigDict, ValidationError

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import PerceptionSettings
from fakeyou.foundation.invariants import POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.polling import PollingLoop, PollSchedule
from fakeyou.foundation.resources import load_bridged_script
from fakeyou.observability.faults import ConsumerSink, create_poll_guard
from fakeyou.observability.journal import Journal
from fakeyou.perception.frames import keyframe_from_jpeg
from fakeyou.perception.meet_protocol import (
    COLLECTIONS_CHANNEL,
    SYNC_COLLECTIONS_PATH,
    CollectionUpdate,
    MeetChatMessage,
    decode_collection_event,
    decode_sync_response,
)
from fakeyou.perception.percepts import IncomingChat, ParticipantRef
from fakeyou.perception.roster import MeetRoster, RosterChange, StreamAssignment
from fakeyou.perception.timeline import MeetingTimeline

PERCEPTION_ASSET = "perception.js"
PERCEPTION_DRAIN_JS = "() => window.__fakeyouPerception ? window.__fakeyouPerception.drain() : null"
PERCEPTION_ASSIGN_JS = (
    "(assignments) => window.__fakeyouPerception "
    "? window.__fakeyouPerception.assign(assignments) : null"
)
PERCEPTION_POLL_SECONDS = 0.5
BACKLOG_GRACE = timedelta(seconds=2)
UNKNOWN_SENDER = "Someone"
Clock = Callable[[], datetime]


def load_perception_script() -> str:
    return load_bridged_script(PERCEPTION_ASSET)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DrainPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class DrainedMessage(DrainPart):
    channel: str
    page_ms: float
    b64: str


class DrainedFrame(DrainPart):
    stream_id: str
    role: Literal["screen", "camera"]
    page_ms: float
    width: int
    height: int
    reason: str
    jpeg_b64: str


class DrainBatch(DrainPart):
    messages: list[DrainedMessage] = []
    frames: list[DrainedFrame] = []
    tracks: list[str] = []
    sampling: list[str] = []
    dropped_messages: int = 0
    dropped_frames: int = 0


def page_time(page_ms: float) -> datetime:
    return datetime.fromtimestamp(page_ms / 1000, UTC)


class ResponseLike(Protocol):
    @property
    def url(self) -> str: ...

    async def body(self) -> bytes: ...


class PerceptionSensors:
    def __init__(
        self,
        timeline: MeetingTimeline,
        settings: PerceptionSettings,
        *,
        joined_at: datetime,
        journal: Journal | None = None,
        clock: Clock = _utc_now,
    ) -> None:
        self.timeline = timeline
        self.roster = MeetRoster()
        self._settings = settings
        self._joined_at = joined_at
        self._journal = journal or Journal("", echo=False)
        self._clock = clock
        self.rejected_messages = 0
        self.frames_seen = 0

    async def accept_batch(self, batch: DrainBatch) -> None:
        for message in batch.messages:
            if message.channel == COLLECTIONS_CHANNEL:
                self._accept_collection(message)
        for frame in batch.frames:
            await self._accept_frame(frame)

    async def on_response(self, response: ResponseLike) -> None:
        if SYNC_COLLECTIONS_PATH not in response.url:
            return
        try:
            body = await response.body()
        except (OSError, RuntimeError):
            return
        self.accept_sync(body)

    def accept_sync(self, body: bytes) -> None:
        try:
            users = decode_sync_response(body)
        except FakeYouError as error:
            self._reject("sync", error)
            return
        self._journal.record(
            "perception_sync_accepted",
            source="perception",
            users=len(users),
            screens=sum(user.is_screen for user in users),
        )
        self._apply_changes(self.roster.apply_users(users, complete=True), self._clock())

    def assignments(self) -> list[StreamAssignment]:
        settings = self._settings
        return [
            item
            for item in self.roster.assignments()
            if (item.role == "screen" and settings.screen)
            or (item.role == "camera" and settings.cameras)
        ]

    def _accept_collection(self, message: DrainedMessage) -> None:
        try:
            update = decode_collection_event(base64.b64decode(message.b64, validate=True))
        except (FakeYouError, binascii.Error) as error:
            self._reject("collection", error)
            return
        self._apply_update(update, page_time(message.page_ms))

    def _apply_update(self, update: CollectionUpdate, at: datetime) -> None:
        changes = self.roster.apply_users(update.users, complete=False)
        changes += self.roster.apply_outputs(update.outputs)
        self._apply_changes(changes, at)
        if self._settings.chat:
            for chat in update.chat:
                self._accept_chat(chat, at)

    def _person(self, device_id: str) -> ParticipantRef:
        return self.timeline.participant(device_id, self.roster.name_of(device_id))

    def _apply_changes(self, changes: Sequence[RosterChange], at: datetime) -> None:
        for change in changes:
            person = self.timeline.participant(change.device_id, change.name)
            presenting = change.kind in {"presenting_started", "presenting_stopped"}
            if presenting or self._settings.signals:
                self.timeline.add_signal(change.kind, person, at)

    def _accept_chat(self, chat: MeetChatMessage, at: datetime) -> None:
        sent_at = chat.sent_at or at
        sender = self._person(chat.device_id)
        if not sender.name:
            sender = sender.model_copy(update={"name": UNKNOWN_SENDER})
        self.timeline.add_chat(
            IncomingChat(
                message_id=chat.message_id,
                sender=sender,
                text=chat.text,
                sent_at=sent_at,
                own=chat.device_id in self.roster.own_device_ids,
                backlog=sent_at < self._joined_at - BACKLOG_GRACE,
            )
        )

    async def _accept_frame(self, frame: DrainedFrame) -> None:
        assignment = self.roster.assignment_for(frame.stream_id)
        if assignment is None or assignment.role != frame.role:
            return
        try:
            data = base64.b64decode(frame.jpeg_b64, validate=True)
            keyframe = await asyncio.to_thread(
                keyframe_from_jpeg, data, captured_at=page_time(frame.page_ms)
            )
        except (binascii.Error, OSError, ValueError) as error:
            self._reject("frame", error)
            return
        self.frames_seen += 1
        person = self._person(assignment.device_id)
        if frame.role == "screen":
            self.timeline.show_screen(keyframe, person, data=data)
        else:
            self.timeline.camera_frames.put(person.participant_id, data)

    def _reject(self, kind: str, error: Exception) -> None:
        self.rejected_messages += 1
        self._journal.record(
            "perception_input_rejected",
            source="perception",
            kind=kind,
            error_type=type(error).__name__,
            rejected=self.rejected_messages,
        )


class PerceptionPage(Protocol):
    async def evaluate(self, expression: str, arg: Any = None, /) -> Any: ...


DEFAULT_PERCEPTION_SCHEDULE = PollSchedule(interval_seconds=PERCEPTION_POLL_SECONDS)


class PerceptionDrain(PollingLoop):
    _COMPENSATE_SLEEP = True

    def __init__(
        self,
        page: PerceptionPage,
        sensors: PerceptionSensors,
        *,
        schedule: PollSchedule = DEFAULT_PERCEPTION_SCHEDULE,
        sinks: ConsumerSink | None = None,
    ) -> None:
        super().__init__(
            poll_interval_seconds=schedule.interval_seconds,
            monotonic=schedule.monotonic,
            guard=create_poll_guard(sinks, source="browser", operation="perception_drain"),
        )
        self._page = page
        self._sensors = sensors
        self._journal = sinks.journal if sinks is not None else None
        self._assigned: tuple[StreamAssignment, ...] = ()
        self.batches_rejected = 0

    @property
    def sensors(self) -> PerceptionSensors:
        return self._sensors

    @override
    async def _poll_once(self) -> None:
        await self.drain_once()

    async def drain_once(self) -> None:
        self.polls += 1
        async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
            raw = await self._page.evaluate(PERCEPTION_DRAIN_JS)
        if raw is None:
            return
        try:
            batch = DrainBatch.model_validate(raw)
        except ValidationError:
            self.batches_rejected += 1
            if self._journal is not None:
                self._journal.record(
                    "perception_batch_rejected", source="perception", rejected=self.batches_rejected
                )
            return
        await self._sensors.accept_batch(batch)
        await self._push_assignments()

    async def _push_assignments(self) -> None:
        wanted = tuple(self._sensors.assignments())
        if wanted == self._assigned:
            return
        payload = [
            {"stream_id": item.stream_id, "role": item.role, "device_id": item.device_id}
            for item in wanted
        ]
        async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
            await self._page.evaluate(PERCEPTION_ASSIGN_JS, payload)
        self._assigned = wanted
