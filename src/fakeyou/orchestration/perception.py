import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.providers import create_perception_chat_model
from fakeyou.perception.budget import PerceptionBudget
from fakeyou.perception.context import PerceptionContext
from fakeyou.perception.interpreter import PerceptionInterpreter, ScreenReader
from fakeyou.perception.percepts import ChatLine, MeetingSignal, ScreenSegment
from fakeyou.perception.roster import MeetRoster
from fakeyou.perception.sensors import PerceptionSensors
from fakeyou.perception.timeline import (
    ChatReceived,
    MeetingTimeline,
    ScreenRead,
    ScreenShown,
    SignalObserved,
    TimelineEvent,
)
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.record import ChatFields, MeetingRecord, ScreenFields, SignalFields


def screen_fields(segment: ScreenSegment) -> ScreenFields | None:
    reading = segment.reading
    if reading is None or reading.is_noise:
        return None
    presenter = segment.presenter
    return ScreenFields(
        ref=segment.ref,
        shown_at=segment.first_seen,
        sha256=segment.keyframe.sha256,
        presenter_id=presenter.participant_id if presenter else None,
        presenter=presenter.name if presenter else None,
        content=reading.content_kind,
        title=reading.title,
        text=reading.text,
        key_points=tuple(reading.key_points),
        figures=tuple(f"{figure.label}: {figure.value}" for figure in reading.figures),
        visuals=tuple(reading.visuals),
        people=tuple(reading.people),
    )


def chat_fields(line: ChatLine) -> ChatFields:
    return ChatFields(
        ref=line.ref,
        message_id=line.message_id,
        sender_id=line.sender.participant_id,
        sender=line.sender.name,
        text=line.text,
        sent_at=line.sent_at,
        own=line.own,
        backlog=line.backlog,
    )


def signal_fields(signal: MeetingSignal) -> SignalFields:
    return SignalFields(
        signal=signal.kind,
        participant_id=signal.participant.participant_id,
        participant=signal.participant.name,
        observed_at=signal.at,
        detail=signal.detail,
    )


@dataclass(slots=True)
class PerceptionArchive:
    timeline: MeetingTimeline
    record: MeetingRecord | None = None
    screens_dir: Path | None = None
    _writes: set[asyncio.Task[None]] = field(default_factory=set)

    def __call__(self, event: TimelineEvent) -> None:
        match event:
            case ScreenShown(segment=segment, change="new"):
                self._save_keyframe(segment)
            case ScreenRead(segment=segment):
                fields = screen_fields(segment)
                if fields is not None and self.record is not None:
                    self.record.add_screen(fields)
            case ChatReceived(line=line) if self.record is not None:
                self.record.add_chat(chat_fields(line))
            case SignalObserved(signal=signal) if self.record is not None:
                self.record.add_signal(signal_fields(signal))
            case _:
                pass

    def _save_keyframe(self, segment: ScreenSegment) -> None:
        directory = self.screens_dir
        data = self.timeline.keyframes.get(segment.keyframe.sha256)
        if directory is None or data is None:
            return
        name = f"{segment.ref.replace('#', '-')}-{segment.keyframe.sha256[:8]}.jpg"
        task = asyncio.create_task(asyncio.to_thread(write_atomically, directory / name, data))
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def settle(self) -> None:
        if self._writes:
            await asyncio.gather(*tuple(self._writes), return_exceptions=True)


@dataclass(frozen=True, slots=True)
class PerceptionStack:
    timeline: MeetingTimeline
    sensors: PerceptionSensors
    context: PerceptionContext
    archive: PerceptionArchive
    reader: ScreenReader | None = None

    async def close(self) -> None:
        if self.reader is not None:
            await self.reader.close()
        await self.archive.settle()


def create_perception_interpreter(
    settings: Settings, *, journal: Journal
) -> PerceptionInterpreter | None:
    perception = settings.perception
    try:
        model = create_perception_chat_model(settings)
    except FakeYouError as error:
        journal.record(
            "perception_interpreter_disabled",
            source="perception",
            error_code=error.code.value,
        )
        return None
    budget = PerceptionBudget(
        budget_usd=perception.budget_usd, max_calls=perception.max_calls, journal=journal
    )
    return PerceptionInterpreter(
        model,
        budget,
        max_concurrent=perception.interpreter.max_concurrent,
        journal=journal,
    )


def create_perception_stack(
    settings: Settings,
    *,
    journal: Journal,
    record: MeetingRecord | None = None,
    screens_dir: Path | None = None,
    on_roster: Callable[[MeetRoster], None] | None = None,
) -> PerceptionStack | None:
    perception = settings.perception
    if not perception.enabled:
        return None
    timeline = MeetingTimeline(journal=journal)
    archive = PerceptionArchive(timeline, record=record, screens_dir=screens_dir)
    timeline.subscribe(archive)
    interpreter = create_perception_interpreter(settings, journal=journal)
    reader = ScreenReader(timeline, interpreter, journal=journal) if interpreter else None
    sensors = PerceptionSensors(timeline, perception, joined_at=datetime.now(UTC), journal=journal)
    sensors.roster_listener = on_roster
    context = PerceptionContext(
        timeline,
        interpreter,
        max_chars=perception.view_max_chars,
        state=sensors.roster.state,
    )
    return PerceptionStack(
        timeline=timeline, sensors=sensors, context=context, archive=archive, reader=reader
    )
