from datetime import timedelta

from fakeyou.perception.percepts import IncomingChat
from fakeyou.perception.timeline import (
    ChatReceived,
    KeyframeCache,
    MeetingTimeline,
    ScreenRead,
    ScreenShown,
    SignalObserved,
    TimelineEvent,
)
from tests._perception_fakes import START, keyframe, reading, shown_timeline, slide_jpeg
from tests._persistence_fakes import RecordingJournal

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])
PRICING = ("Pricing Plans", ["Starter: 99,000 per month", "Pro: 299,000 per month"])
ROADMAP = ("Product Roadmap 2027", ["Q1: Foundation and security", "Q2: Expansion"])


def test_each_distinct_slide_gets_the_next_screen_reference() -> None:
    timeline = shown_timeline(TIMELINE, PRICING, ROADMAP)
    snapshot = timeline.snapshot()

    assert [screen.ref for screen in snapshot.screens] == ["screen#1", "screen#2", "screen#3"]
    assert snapshot.current_ref == "screen#3"
    assert snapshot.current_screen is not None
    assert snapshot.current_screen.showing
    assert [screen.showing for screen in snapshot.screens] == [False, False, True]


def test_the_same_slide_again_is_not_a_new_screen() -> None:
    timeline = MeetingTimeline()
    data = slide_jpeg(*TIMELINE)

    first = timeline.show_screen(keyframe(data), None, data=data)
    again = timeline.show_screen(keyframe(slide_jpeg(*TIMELINE, quality=70), seconds=2), None)

    assert (first.change, again.change) == ("new", "same")
    assert len(timeline.snapshot().screens) == 1


def test_going_back_to_an_earlier_slide_is_a_revisit_of_its_reference() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    back = timeline.show_screen(keyframe(slide_jpeg(*TIMELINE), seconds=90), None)
    snapshot = timeline.snapshot()

    assert back.change == "revisit"
    assert back.segment.ref == "screen#1"
    assert snapshot.current_ref == "screen#1"
    assert len(back.segment.visits) == 2
    previous = snapshot.previous_screen()
    assert previous is not None
    assert previous.ref == "screen#2"


def test_previous_screen_follows_the_order_screens_were_last_seen() -> None:
    timeline = shown_timeline(TIMELINE, PRICING, ROADMAP)
    timeline.show_screen(keyframe(slide_jpeg(*TIMELINE), seconds=120), None)

    order = [screen.ref for screen in timeline.snapshot().screens_by_recency()]

    assert order == ["screen#1", "screen#3", "screen#2"]


def test_stopping_the_share_closes_the_current_visit() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.stop_screen(START + timedelta(seconds=40))
    snapshot = timeline.snapshot()

    assert snapshot.current_ref is None
    assert snapshot.screens[0].last_seen == START + timedelta(seconds=40)
    assert snapshot.previous_screen() is not None


def test_a_reading_with_the_same_content_merges_into_the_known_screen() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Alpha 12 Jan Beta 17 Mar"))
    timeline.read_screen("screen#2", reading("Pricing Plans", text="Starter 99 Pro 299"))
    blurry = slide_jpeg(*TIMELINE, size=(640, 360), quality=35)
    shown = timeline.show_screen(keyframe(blurry, seconds=90), None, data=blurry)
    events: list[TimelineEvent] = []
    timeline.subscribe(events.append)

    merged = timeline.read_screen(
        shown.segment.ref, reading("launch  timeline", text="Alpha 12 Jan Beta 17 Mar")
    )
    snapshot = timeline.snapshot()

    assert shown.change == "new"
    assert merged is not None
    assert merged.ref == "screen#1"
    assert [screen.ref for screen in snapshot.screens] == ["screen#1", "screen#2"]
    assert snapshot.current_ref == "screen#1"
    assert snapshot.screen(shown.segment.ref) == merged
    assert len(merged.visits) == 2
    assert [type(event) for event in events] == [ScreenShown, ScreenRead]


def test_a_reading_that_differs_keeps_its_own_screen() -> None:
    timeline = shown_timeline(TIMELINE, (TIMELINE[0], ["Starter: 99,000", "Pro: 299,000"]))
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Alpha 12 Jan Beta 17 Mar"))

    kept = timeline.read_screen("screen#2", reading("Launch Timeline", text="Starter 99 Pro 299"))

    assert kept is not None
    assert kept.ref == "screen#2"
    assert len(timeline.snapshot().screens) == 2


def test_noise_readings_never_merge() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    timeline.read_screen("screen#1", reading("Meet", kind="meeting_ui"))

    kept = timeline.read_screen("screen#2", reading("Meet", kind="meeting_ui"))

    assert kept is not None
    assert kept.ref == "screen#2"


def test_a_failed_reading_is_remembered_without_an_event() -> None:
    timeline = shown_timeline(TIMELINE)
    events: list[TimelineEvent] = []
    timeline.subscribe(events.append)

    failed = timeline.read_screen("screen#1", None)

    assert failed is not None
    assert failed.reading_failed
    assert events == []


def test_chat_lines_are_deduplicated_by_message_id_and_numbered() -> None:
    journal = RecordingJournal()
    timeline = MeetingTimeline(journal=journal)
    budi = timeline.participant("device-budi", "Budi")
    events: list[TimelineEvent] = []
    timeline.subscribe(events.append)

    first = timeline.add_chat(
        IncomingChat(message_id="m1", sender=budi, text="deck link?", sent_at=START)
    )
    again = timeline.add_chat(
        IncomingChat(message_id="m1", sender=budi, text="deck link?", sent_at=START)
    )
    blank = timeline.add_chat(IncomingChat(message_id="m2", sender=budi, text=" ", sent_at=START))

    assert first is not None
    assert first.ref == "chat#1"
    assert (again, blank) == (None, None)
    assert [type(event) for event in events] == [ChatReceived]
    assert journal.events("chat_line_observed")[0]["chars"] == len("deck link?")


def test_participants_keep_their_reference_when_renamed() -> None:
    timeline = MeetingTimeline()

    ayu = timeline.participant("device-ayu", "Ayu")
    budi = timeline.participant("device-budi", "Budi")
    renamed = timeline.participant("device-ayu", "Ayu Lestari")
    unnamed = timeline.participant("device-ayu", "")

    assert (ayu.ref, budi.ref, renamed.ref) == ("person#1", "person#2", "person#1")
    assert unnamed.name == "Ayu Lestari"


def test_presenting_signals_track_the_presenter_and_close_the_screen() -> None:
    timeline = shown_timeline(TIMELINE)
    ayu = timeline.participant("device-ayu", "Ayu")
    events: list[TimelineEvent] = []
    timeline.subscribe(events.append)

    timeline.add_signal("presenting_started", ayu, START)
    assert timeline.snapshot().presenting == ayu
    timeline.add_signal("presenting_stopped", ayu, START + timedelta(minutes=5))
    snapshot = timeline.snapshot()

    assert snapshot.presenting is None
    assert snapshot.current_ref is None
    assert [type(event) for event in events] == [SignalObserved, SignalObserved]


def test_keyframe_cache_evicts_the_least_recently_used_frame() -> None:
    cache = KeyframeCache(capacity=2)
    cache.put("a", b"1")
    cache.put("b", b"2")
    assert cache.get("a") == b"1"
    cache.put("c", b"3")

    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (b"1", None, b"3")


def test_screen_captures_are_journaled_without_content() -> None:
    journal = RecordingJournal()
    timeline = MeetingTimeline(journal=journal)
    data = slide_jpeg(*TIMELINE)

    timeline.show_screen(keyframe(data), None, data=data)

    [event] = journal.events("screen_captured")
    assert event["source"] == "perception"
    assert (event["ref"], event["change"], event["width"]) == ("screen#1", "new", 1280)


def test_a_non_meeting_reading_is_stored_title_only() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen(
        "screen#1",
        reading(
            "Cartesia Usage",
            text="Receipt 1450-6289 Rp899,900",
            kind="dashboard",
            key_points=("Receipt 1450-6289 Rp899,900",),
            figures=(("amount paid", "Rp899,900"),),
        ),
    )

    stored = timeline.snapshot().screens[0].reading

    assert stored is not None
    assert stored.title == "Cartesia Usage"
    assert stored.content_kind == "dashboard"
    assert stored.text == ""
    assert stored.key_points == []
    assert stored.figures == []
    assert stored.visuals == []
    assert stored.people == []


def test_the_same_non_meeting_screen_again_merges_by_title() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    timeline.read_screen(
        "screen#1", reading("Cartesia Usage", text="Receipt 1450-6289", kind="dashboard")
    )
    timeline.read_screen(
        "screen#2", reading("Cartesia Usage", text="Invoice 7777-1111", kind="dashboard")
    )

    assert [screen.ref for screen in timeline.snapshot().screens] == ["screen#1"]
