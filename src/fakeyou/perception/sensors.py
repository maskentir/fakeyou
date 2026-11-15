import asyncio
import base64
import binascii
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, override

from pydantic import BaseModel, ConfigDict, ValidationError

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import PerceptionSettings
from fakeyou.foundation.clock_mapping import ClockExchange, MappedInstant, RemoteClock
from fakeyou.foundation.content_digest import ContentSample, DigestRecord
from fakeyou.foundation.invariants import PERCEPTION_POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.polling import PollingLoop, PollSchedule
from fakeyou.foundation.resources import load_bridged_script
from fakeyou.observability.faults import ConsumerSink, create_poll_guard
from fakeyou.observability.journal import Journal
from fakeyou.perception.frames import keyframe_from_jpeg
from fakeyou.perception.meet_protocol import (
    COLLECTIONS_CHANNEL,
    SYNC_COLLECTIONS_PATH,
    CollectionUpdate,
    DeviceOutput,
    MeetChatMessage,
    decode_collection_event,
    decode_sync_response,
)
from fakeyou.perception.percepts import IncomingChat, ParticipantRef
from fakeyou.perception.roster import MeetRoster, RosterChange, StreamAssignment
from fakeyou.perception.timeline import (
    SCREEN_SOURCE_ID,
    CameraSample,
    MeetingTimeline,
)

PERCEPTION_ASSET = "perception.js"
PERCEPTION_DRAIN_JS = "() => window.__fakeyouPerception ? window.__fakeyouPerception.drain() : null"
PERCEPTION_ASSIGN_JS = (
    "(assignments) => window.__fakeyouPerception "
    "? window.__fakeyouPerception.assign(assignments) : null"
)
PERCEPTION_POLL_SECONDS = 0.5
FRAME_SAMPLE_CACHE_SIZE = 512
DRAIN_STATS_EVERY = 30
BACKLOG_GRACE = timedelta(seconds=2)
BACKLOG_WINDOW = timedelta(seconds=45)
_PLAUSIBLE_SKEW = timedelta(seconds=120)
UNKNOWN_SENDER = "Someone"
Clock = Callable[[], datetime]


def load_perception_script() -> str:
    return load_bridged_script(PERCEPTION_ASSET)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DrainPart(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    page_session: str | None = None


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
    sequence: int | None = None
    generation: int = 0


class DrainedSample(DrainPart):
    stream_id: str
    role: Literal["screen", "camera"]
    page_ms: float
    sequence: int
    media_timestamp: float | None = None
    width: int = 0
    height: int = 0
    pixel_format: str = ""
    algorithm: str = ""
    version: int = 0
    digest: str | None = None
    skip_reason: str | None = None
    generation: int = 0


class ActiveSampler(DrainPart):
    stream_id: str
    role: Literal["screen", "camera"]
    last_sample_page_ms: float = 0
    generation: int = 0


class DrainBatch(DrainPart):
    messages: list[DrainedMessage] = []
    frames: list[DrainedFrame] = []
    samples: list[DrainedSample] = []
    tracks: list[str] = []
    sampling: list[str] = []
    active: list[ActiveSampler] = []
    peers: list[str] = []
    dropped_messages: int = 0
    dropped_frames: int = 0
    dropped_samples: int = 0
    drained_page_ms: float = 0


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
        on_camera_invalidate: Callable[[str | None], None] | None = None,
    ) -> None:
        self.timeline = timeline
        self.monotonic: Callable[[], float] = time.monotonic
        self.page_clock = RemoteClock(now=lambda: self.monotonic())
        self.roster = MeetRoster()
        self.roster_listener: Callable[[MeetRoster], None] | None = None
        self._settings = settings
        self._joined_at = joined_at
        self._journal = journal or Journal("", echo=False)
        self._clock = clock
        self._on_camera_invalidate = on_camera_invalidate
        self.epoch = 0
        self.page_session: str | None = None
        self._retired_pages: set[str] = set()
        self.rejected_messages = 0
        self.heartbeats = 0
        self.frames_seen = 0
        self.drained_at: float | None = None
        self.synced_at: float | None = None
        self.peer_states: tuple[str, ...] = ()
        self._own_status: int | None = None
        self._audio_outputs: set[tuple[str, str]] = set()
        self._admitted_at: datetime | None = None
        self._sampler_samples: dict[str, float] = {}
        self._frame_samples: OrderedDict[tuple[int, str | None, str, int, int], ContentSample] = (
            OrderedDict()
        )
        self._recording_streams: set[str] = set()
        self.samples_seen = 0
        self.samples_irregular = 0

    def _invalidate_camera(self, participant_id: str | None = None) -> None:
        if self._on_camera_invalidate is not None:
            self._on_camera_invalidate(participant_id)

    def _camera_assignments(self) -> dict[str, str]:
        return {
            item.device_id: item.stream_id
            for item in self.roster.assignments()
            if item.role == "camera"
        }

    def _invalidate_changed_assignments(self, before: Mapping[str, str]) -> None:
        after = self._camera_assignments()
        for device_id, stream_id in before.items():
            if after.get(device_id) == stream_id:
                continue
            self.timeline.camera_frames.drop(device_id)
            self._invalidate_camera(device_id)

    def capture_instant(self, captured_at: datetime) -> MappedInstant | None:
        return self.page_clock.map(captured_at.timestamp() * 1000)

    def _page_refusal(self, batch: DrainBatch, expected_epoch: int | None) -> str | None:
        if expected_epoch is not None and expected_epoch != self.epoch:
            return "page_epoch_changed"
        session = batch.page_session
        if session in self._retired_pages:
            return "page_session_retired"
        if session == "" or (
            session is None
            and (self.page_session is not None or (expected_epoch is not None and self.epoch > 0))
        ):
            return "page_session_missing"
        parts = (*batch.messages, *batch.samples, *batch.frames, *batch.active)
        if any(item.page_session != session for item in parts):
            return "page_session_mismatch"
        if (
            self.page_session is not None
            and session != self.page_session
            and expected_epoch is None
        ):
            return "page_transition_unannounced"
        return None

    def _reject_batch(self, reason: str) -> bool:
        self._journal.record(
            "perception_batch_rejected", source="perception", reason=reason, epoch=self.epoch
        )
        return False

    async def accept_batch(
        self,
        batch: DrainBatch,
        *,
        expected_epoch: int | None = None,
        exchange: ClockExchange | None = None,
    ) -> bool:
        refusal = self._page_refusal(batch, expected_epoch)
        if refusal is not None:
            return self._reject_batch(refusal)
        if self.page_session is not None and batch.page_session != self.page_session:
            self.begin_epoch()
        if batch.page_session != self.page_session:
            self.timeline.samples.begin_page(self.epoch, batch.page_session)
            self.page_session = batch.page_session
            self._frame_samples.clear()
            self._recording_streams.clear()
            self._sampler_samples.clear()
        epoch = self.epoch
        if exchange is not None:
            self.page_clock.observe(exchange)
        self.drained_at = self.monotonic()
        self._observe_peers(tuple(batch.peers))
        for message in batch.messages:
            if message.channel == COLLECTIONS_CHANNEL:
                self._accept_collection(message)
        for sample in batch.samples:
            self._accept_sample(sample)
        self._touch_active(batch.active)
        for frame in batch.frames:
            if self.epoch != epoch or self.page_session != batch.page_session:
                return self._reject_batch("page_epoch_changed")
            await self._accept_frame(frame)
        return self.epoch == epoch and self.page_session == batch.page_session

    def _accept_sample(self, sample: DrainedSample) -> None:
        assignment = self.roster.assignment_for(sample.stream_id)
        if assignment is None or assignment.role != sample.role:
            return
        subject_id = (
            SCREEN_SOURCE_ID
            if sample.role == "screen"
            else self._person(assignment.device_id).participant_id
        )
        captured_at = page_time(sample.page_ms)
        capture = self.capture_instant(captured_at)
        ingested = self.timeline.confirm(
            DigestRecord(
                algorithm=sample.algorithm,
                version=sample.version,
                width=sample.width,
                height=sample.height,
                pixel_format=sample.pixel_format,
                role=sample.role,
                stream_id=sample.stream_id,
                subject_id=subject_id,
                sequence=sample.sequence,
                media_timestamp=sample.media_timestamp,
                digest=sample.digest,
                skip_reason=sample.skip_reason,
                generation=sample.generation,
                page_epoch=self.epoch,
                page_session=sample.page_session,
            ),
            captured_at=captured_at,
            capture=capture,
        )
        self._recording_streams.add(sample.stream_id)
        if ingested.outcome in {"accepted", "inconsistent_time"}:
            self._frame_samples[
                self.epoch,
                sample.page_session,
                sample.stream_id,
                sample.generation,
                sample.sequence,
            ] = ingested.sample
        while len(self._frame_samples) > FRAME_SAMPLE_CACHE_SIZE:
            self._frame_samples.popitem(last=False)
        self.samples_seen += 1
        outcome = ingested.outcome
        if outcome != "accepted":
            self.samples_irregular += 1
            self._journal.record(
                "perception_sample_rejected",
                source="perception",
                stream_id=sample.stream_id,
                role=sample.role,
                sequence=sample.sequence,
                outcome=outcome,
            )

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
        self.roster.mark_synced()
        self.synced_at = self.monotonic()
        self.heartbeats += 1
        before = self._camera_assignments()
        changes = self.roster.apply_snapshot(users)
        self._invalidate_changed_assignments(before)
        self._apply_changes(changes, self._clock())
        self._observe_own_status()
        self._share_roster()

    def drained_within(self, seconds: float) -> bool:
        return self.drained_at is not None and self.monotonic() - self.drained_at <= seconds

    def synced_within(self, seconds: float) -> bool:
        return self.synced_at is not None and self.monotonic() - self.synced_at <= seconds

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
        before = self._camera_assignments()
        self._journal_audio_outputs(update.outputs)
        if update.skipped:
            self._journal.record(
                "perception_output_skipped",
                source="perception",
                devices=list(update.skipped),
                skipped=len(update.skipped),
            )
        changes = self.roster.apply_users(update.users)
        changes += self.roster.apply_outputs(update.outputs)
        self._invalidate_changed_assignments(before)
        self._apply_changes(changes, at)
        self._observe_own_status()
        if update.users or update.outputs:
            self.heartbeats += 1
            self._share_roster()
        if self._settings.chat:
            for chat in update.chat:
                self._accept_chat(chat, at)

    def _observe_peers(self, states: tuple[str, ...]) -> None:
        if states == self.peer_states:
            return
        self.peer_states = states
        self._journal.record(
            "perception_peer_state_observed", source="perception", states=list(states)
        )

    def _observe_own_status(self) -> None:
        own = self.roster.own_user()
        if own is None or own.status == self._own_status:
            return
        self._own_status = own.status
        self._journal.record(
            "perception_own_status_observed",
            source="perception",
            status=own.status,
            present=own.present,
        )

    def _share_roster(self) -> None:
        if self.roster_listener is not None:
            self.roster_listener(self.roster)

    def _journal_audio_outputs(self, outputs: Sequence[DeviceOutput]) -> None:
        for output in outputs:
            key = (output.device_id, output.stream_id)
            if output.kind != "audio" or key in self._audio_outputs:
                continue
            self._audio_outputs.add(key)
            self._journal.record(
                "perception_audio_output_observed",
                source="perception",
                device_id=output.device_id,
                stream_id=output.stream_id,
                own=output.device_id in self.roster.own_device_ids,
            )

    def _person(self, device_id: str) -> ParticipantRef:
        return self.timeline.participant(device_id, self.roster.name_of(device_id))

    def _apply_changes(self, changes: Sequence[RosterChange], at: datetime) -> None:
        for change in changes:
            person = self.timeline.participant(change.device_id, change.name)
            presenting = change.kind in {"presenting_started", "presenting_stopped"}
            if presenting or self._settings.signals:
                self.timeline.add_signal(change.kind, person, at)
            if change.kind == "left":
                self.timeline.camera_frames.drop(change.device_id)
                self._invalidate_camera(change.device_id)
                self.timeline.leave(change.device_id)
            if change.kind == "camera_off":
                self.timeline.camera_frames.drop(change.device_id)
                self._invalidate_camera(change.device_id)

    def begin_epoch(self) -> None:
        if self.page_session is not None:
            self._retired_pages.add(self.page_session)
        self.epoch += 1
        self.page_session = None
        self.timeline.camera_frames.clear()
        self.timeline.stop_screen(self._clock())
        self.timeline.samples.begin_page(self.epoch)
        self.page_clock = RemoteClock(now=lambda: self.monotonic())
        self._frame_samples.clear()
        self._recording_streams.clear()
        self._sampler_samples.clear()
        self._invalidate_camera(None)
        self.roster.begin_epoch()
        self.drained_at = None
        self.synced_at = None
        self.heartbeats = 0
        self.peer_states = ()
        self._own_status = None

    def _touch_active(self, active: Sequence[ActiveSampler]) -> None:
        seen = self.monotonic()
        for sampler in active:
            if sampler.role != "camera":
                continue
            assignment = self.roster.assignment_for(sampler.stream_id)
            if assignment is None or assignment.role != "camera":
                continue
            frame = self.timeline.camera_frames.get(assignment.device_id)
            if (
                frame is None
                or frame.ref.stream_id != sampler.stream_id
                or frame.ref.epoch != self.epoch
            ):
                continue
            if not self._sampling_advanced(sampler):
                continue
            if sampler.generation > 0:
                latest = self.timeline.samples.latest("camera", frame.ref.subject_id)
                if (
                    latest is None
                    or frame.content is None
                    or latest.record.generation != sampler.generation
                    or frame.content.record.generation != sampler.generation
                ):
                    continue
            self.timeline.camera_frames.touch(assignment.device_id, seen_at=seen)
            if sampler.stream_id not in self._recording_streams:
                self.timeline.camera_frames.unverified_heartbeat(
                    assignment.device_id, captured_at=page_time(sampler.last_sample_page_ms)
                )

    def _sampling_advanced(self, sampler: ActiveSampler) -> bool:
        if sampler.last_sample_page_ms <= 0:
            return False
        previous = self._sampler_samples.get(sampler.stream_id)
        if previous is not None and sampler.last_sample_page_ms <= previous:
            return False
        self._sampler_samples[sampler.stream_id] = sampler.last_sample_page_ms
        return True

    def mark_admitted(self, at: datetime) -> None:
        if self._admitted_at is None:
            self._admitted_at = at

    def _classify_chat(self, chat: MeetChatMessage, at: datetime) -> tuple[datetime, bool]:
        parsed = chat.sent_at
        anchor = self._admitted_at if self._admitted_at is not None else self._joined_at
        early = at <= anchor + BACKLOG_WINDOW
        if early and parsed is not None and parsed < anchor - BACKLOG_GRACE:
            return parsed, True
        if parsed is not None and at - _PLAUSIBLE_SKEW <= parsed <= at + _PLAUSIBLE_SKEW:
            return parsed, False
        return at, False

    def _accept_chat(self, chat: MeetChatMessage, at: datetime) -> None:
        sent_at, backlog = self._classify_chat(chat, at)
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
                backlog=backlog,
            )
        )

    async def _accept_frame(self, frame: DrainedFrame) -> None:
        epoch = self.epoch
        page_session = self.page_session
        assignment = self.roster.assignment_for(frame.stream_id)
        if assignment is None or assignment.role != frame.role:
            self._journal.record(
                "perception_frame_unassigned",
                source="perception",
                stream_id=frame.stream_id,
                role=frame.role,
                assignments=sorted(
                    f"{item.role}:{item.stream_id}" for item in self.roster.assignments()
                ),
            )
            return
        device_id = assignment.device_id
        revision = self.timeline.camera_frames.current_generation(device_id)
        try:
            data = base64.b64decode(frame.jpeg_b64, validate=True)
            keyframe = await asyncio.to_thread(
                keyframe_from_jpeg, data, captured_at=page_time(frame.page_ms)
            )
        except (binascii.Error, OSError, ValueError) as error:
            self._reject("frame", error)
            return
        if (
            self.epoch != epoch
            or self.page_session != page_session
            or self.timeline.camera_frames.current_generation(device_id) != revision
            or not self._assignment_current(frame, device_id)
        ):
            self._journal.record(
                "perception_frame_dropped",
                source="perception",
                stream_id=frame.stream_id,
                role=frame.role,
                epoch=epoch,
                current_epoch=self.epoch,
            )
            return
        self.frames_seen += 1
        person = self._person(device_id)
        sample = (
            None
            if frame.sequence is None
            else self._frame_samples.get(
                (epoch, page_session, frame.stream_id, frame.generation, frame.sequence)
            )
        )
        if frame.generation > 0:
            latest = self.timeline.samples.latest(
                frame.role, person.participant_id if frame.role == "camera" else SCREEN_SOURCE_ID
            )
            if (
                sample is None
                or latest is None
                or latest.record.sampler_identity
                != (epoch, page_session, frame.stream_id, frame.generation)
                or self.timeline.samples.stopped(frame.role, latest.record.subject_id)
            ):
                self._journal.record(
                    "perception_frame_dropped",
                    source="perception",
                    reason="retired_stream",
                    stream_id=frame.stream_id,
                    generation=frame.generation,
                )
                return
        if frame.role == "screen":
            self.timeline.show_screen(keyframe, person, data=data, sample=sample)
        else:
            participant_id = person.participant_id
            current = self.timeline.camera_frames.get(participant_id)
            if current is not None and current.ref.stream_id != frame.stream_id:
                self._invalidate_camera(participant_id)
            self.timeline.camera_frames.put(
                participant_id,
                CameraSample(
                    data=data,
                    stream_id=frame.stream_id,
                    captured_at=page_time(frame.page_ms),
                    epoch=self.epoch,
                    seen_at=self.monotonic(),
                    sample=sample,
                ),
            )

    def _assignment_current(self, frame: DrainedFrame, device_id: str) -> bool:
        assignment = self.roster.assignment_for(frame.stream_id)
        if assignment is None or assignment.device_id != device_id:
            return False
        if assignment.role != frame.role:
            return False
        return device_id in self.roster.people()

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
        self._assigned_page = (sensors.epoch, sensors.page_session)
        self.batches_rejected = 0

    @property
    def sensors(self) -> PerceptionSensors:
        return self._sensors

    @override
    async def _poll_once(self) -> None:
        await self.drain_once()

    async def drain_once(self) -> None:
        self.polls += 1
        sent = self._sensors.monotonic()
        expected_epoch = self._sensors.epoch
        async with asyncio.timeout(PERCEPTION_POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
            raw = await self._page.evaluate(PERCEPTION_DRAIN_JS)
        received = self._sensors.monotonic()
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
        accepted = await self._sensors.accept_batch(
            batch,
            expected_epoch=expected_epoch,
            exchange=ClockExchange(batch.drained_page_ms, sent, received),
        )
        if not accepted:
            self.batches_rejected += 1
            return
        if self._journal is not None and (
            batch.frames or batch.dropped_frames or self.polls % DRAIN_STATS_EVERY == 0
        ):
            self._journal.record(
                "perception_batch_observed",
                source="perception",
                frames=len(batch.frames),
                camera_frames=sum(item.role == "camera" for item in batch.frames),
                screen_frames=sum(item.role == "screen" for item in batch.frames),
                tracks=len(batch.tracks),
                track_ids=list(batch.tracks),
                sampling=len(batch.sampling),
                output_streams=self._sensors.roster.output_streams(),
                peers=len(batch.peers),
                dropped_frames=batch.dropped_frames,
                dropped_messages=batch.dropped_messages,
            )
        await self._push_assignments()

    async def _push_assignments(self) -> None:
        wanted = tuple(self._sensors.assignments())
        page_identity = (self._sensors.epoch, self._sensors.page_session)
        if wanted == self._assigned and page_identity == self._assigned_page:
            return
        payload = [
            {"stream_id": item.stream_id, "role": item.role, "device_id": item.device_id}
            for item in wanted
        ]
        async with asyncio.timeout(PERCEPTION_POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
            await self._page.evaluate(PERCEPTION_ASSIGN_JS, payload)
        if page_identity != (self._sensors.epoch, self._sensors.page_session):
            return
        self._assigned = wanted
        self._assigned_page = page_identity
        if self._journal is not None:
            self._journal.record(
                "perception_streams_bound",
                source="perception",
                streams=[f"{item.role}:{item.stream_id}" for item in wanted],
            )
