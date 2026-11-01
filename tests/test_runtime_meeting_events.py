import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.orchestration.kernel_port import HeardChat, KernelPort, TurnSignal
from fakeyou.orchestration.runtime import MeetingWorker
from fakeyou.perception.percepts import ChatLine, IncomingChat, ParticipantRef, ScreenSegment
from fakeyou.perception.timeline import MeetingTimeline
from tests._conversation_fakes import FakeConversation
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page
from tests._perception_fakes import keyframe, reading, slide_jpeg
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import session_config


def _worker(
    clock: FakeClock, journal: RecordingJournal, timeline: MeetingTimeline, **config: Any
) -> MeetingWorker:
    worker = MeetingWorker(
        session_config(**config),
        meet_page([]),
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
        journal=journal,
    )
    worker.conversation = cast(Any, FakeConversation([]))
    worker.perception = cast(
        Any,
        SimpleNamespace(
            timeline=timeline,
            sensors=SimpleNamespace(heartbeats=0, roster=SimpleNamespace(state=lambda: None)),
            close=_closed,
        ),
    )
    return worker


def _screen(
    timeline: MeetingTimeline, title: str, *, key_points: Sequence[str] = (), kind: str = "slide"
) -> ScreenSegment:
    shown = timeline.show_screen(keyframe(slide_jpeg(title)), None)
    timeline.read_screen(shown.segment.ref, reading(title, key_points=key_points, kind=kind))
    return shown.segment


async def _closed() -> None:
    return None


def _chat(timeline: MeetingTimeline, text: str, *, own: bool = False) -> ChatLine:
    sender = ParticipantRef(ref="person#1", participant_id="devices/ayu", name="Ayu")
    line = timeline.add_chat(
        IncomingChat(
            message_id=f"m-{text}",
            sender=sender,
            text=text,
            sent_at=datetime.now(UTC),
            own=own,
        )
    )
    assert line is not None
    return line


class HeardChats:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def accept(self, signal: TurnSignal) -> None:
        if isinstance(signal, HeardChat):
            self.texts.append(signal.event.text)

    async def idle(self) -> None:
        return None


def _heard_chats(worker: MeetingWorker) -> HeardChats:
    kernel = KernelPort.for_meeting("meeting-9")
    heard = HeardChats()
    kernel.on_turn(heard)
    worker.kernel = kernel
    return heard


async def test_a_chat_line_reaches_the_turns_through_the_kernel(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline)
    heard = _heard_chats(worker)
    worker.watch_meeting_events()

    _chat(timeline, "Rani, can you share the deck?")
    await cast(KernelPort, worker.kernel).settled()

    assert heard.texts == ["Rani, can you share the deck?"]
    assert cast(FakeConversation, worker.conversation).chat == []
    assert journal.events("chat_line_observed")


async def test_the_twin_own_chat_is_not_answered(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline)
    heard = _heard_chats(worker)
    worker.watch_meeting_events()

    _chat(timeline, "Rani, here is the deck", own=True)
    await cast(KernelPort, worker.kernel).settled()

    assert heard.texts == []
    assert [line.text for line in cast(KernelPort, worker.kernel).state.dialogue.lines] == [
        "Rani, here is the deck"
    ]


async def test_the_twin_is_offered_the_room_and_the_rate_limit_holds(
    journal: RecordingJournal,
) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(
        clock, journal, timeline, attention_enabled=True, attention_max_interjections=1
    )

    _screen(timeline, "Beta Date", key_points=("the beta date looks wrong",))
    await worker.offer_attention()
    _screen(timeline, "Beta Date Two", key_points=("the beta date still looks wrong",))
    await worker.offer_attention()

    conversation = cast(FakeConversation, worker.conversation)
    assert len(conversation.attention) == 1
    assert journal.events("attention_observed")


async def test_the_same_screen_is_never_offered_twice(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(
        clock, journal, timeline, attention_enabled=True, attention_max_interjections=5
    )

    _screen(timeline, "Beta Date", key_points=("the beta date looks wrong",))
    await worker.offer_attention()
    await worker.offer_attention()
    await worker.offer_attention()

    assert len(cast(FakeConversation, worker.conversation).attention) == 1
    assert len(journal.events("attention_observed")) == 1


async def test_a_chat_line_is_never_read_out_loud(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline, attention_enabled=True)

    _chat(timeline, "the beta date looks wrong")
    await worker.offer_attention()

    assert cast(FakeConversation, worker.conversation).attention == []


async def test_the_offer_names_the_screen_without_asking_a_question(
    journal: RecordingJournal,
) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline, attention_enabled=True)

    _screen(timeline, "Beta Date", key_points=("the beta date looks wrong",))
    await worker.offer_attention()

    [offered] = cast(FakeConversation, worker.conversation).attention
    assert "Beta Date" in offered
    assert "?" not in offered


async def test_nothing_to_say_means_no_interjection(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline, attention_enabled=True)

    await worker.offer_attention()

    assert cast(FakeConversation, worker.conversation).attention == []


async def test_a_non_meeting_screen_is_offered_by_title_only(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline, attention_enabled=True)

    _screen(
        timeline, "Cartesia Usage", key_points=("Receipt 1450-6289 Rp899,900",), kind="dashboard"
    )
    await worker.offer_attention()

    [offered] = cast(FakeConversation, worker.conversation).attention
    assert "Cartesia Usage" in offered
    assert "1450-6289" not in offered


async def test_the_current_screen_reading_reaches_the_kernel(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline)
    worker.kernel = KernelPort.for_meeting("meeting-9")
    worker.watch_meeting_events()

    shown = timeline.show_screen(keyframe(slide_jpeg("Q4 roadmap")), None)
    worker.kernel.drain()
    pending = worker.kernel.state.world.screen
    timeline.read_screen(shown.segment.ref, reading("Q4 roadmap"))
    worker.kernel.drain()

    assert pending is not None
    assert pending.title is None
    screen = worker.kernel.state.world.screen
    assert screen is not None
    assert (screen.title, screen.reading_ref) == ("Q4 roadmap", shown.segment.ref)
