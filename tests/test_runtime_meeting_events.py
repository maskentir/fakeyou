import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from fakeyou.meet.meet import DEFAULT_MEET_SIGNALS
from fakeyou.orchestration.runtime import MeetingWorker
from fakeyou.perception.percepts import ChatLine, IncomingChat, ParticipantRef
from fakeyou.perception.timeline import MeetingTimeline
from tests._conversation_fakes import FakeConversation
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import meet_page
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


async def test_a_chat_line_reaches_the_conversation(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline)
    worker.watch_meeting_events()

    _chat(timeline, "Rani, can you share the deck?")
    await asyncio.sleep(0)

    conversation = cast(FakeConversation, worker.conversation)
    assert conversation.chat == ["Rani, can you share the deck?"]
    assert journal.events("chat_line_observed")


async def test_the_twin_own_chat_is_not_answered(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline)
    worker.watch_meeting_events()

    _chat(timeline, "Rani, here is the deck", own=True)
    await asyncio.sleep(0)

    assert cast(FakeConversation, worker.conversation).chat == []


async def test_the_twin_is_offered_the_room_and_the_rate_limit_holds(
    journal: RecordingJournal,
) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(
        clock, journal, timeline, attention_enabled=True, attention_max_interjections=1
    )

    _chat(timeline, "the beta date looks wrong")
    await worker.offer_attention()
    await worker.offer_attention()

    conversation = cast(FakeConversation, worker.conversation)
    assert len(conversation.attention) == 1
    assert journal.events("attention_observed")


async def test_nothing_to_say_means_no_interjection(journal: RecordingJournal) -> None:
    clock = FakeClock()
    timeline = MeetingTimeline(journal=journal)
    worker = _worker(clock, journal, timeline, attention_enabled=True)

    await worker.offer_attention()

    assert cast(FakeConversation, worker.conversation).attention == []
