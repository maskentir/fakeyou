import json
from datetime import timedelta

from fakeyou.perception.percepts import CameraGlance, IncomingChat
from fakeyou.perception.timeline import MeetingTimeline
from fakeyou.perception.view import (
    VIEW_PREFIX,
    build_view,
    fit_view,
    render_view,
    resolve_participant,
    resolve_screen,
)
from tests._perception_fakes import START, reading, shown_timeline

TIMELINE = ("Launch Timeline", ["Alpha: 12 January 2027", "Beta: 17 March 2027"])
PRICING = ("Pricing Plans", ["Starter: 99,000 per month", "Pro: 299,000 per month"])
ROADMAP = ("Product Roadmap 2027", ["Q1: Foundation and security", "Q2: Expansion"])
NOW = START + timedelta(minutes=2)


def _read_all(timeline: MeetingTimeline) -> MeetingTimeline:
    timeline.read_screen(
        "screen#1",
        reading(
            "Launch Timeline",
            text="Alpha 12 January 2027. Beta 17 March 2027.",
            figures=[("Beta", "17 March 2027")],
        ),
    )
    timeline.read_screen("screen#2", reading("Pricing Plans", text="Pro 299,000 per month"))
    timeline.read_screen("screen#3", reading("Product Roadmap 2027", text="Q1 Foundation"))
    return timeline


def test_the_view_shows_the_current_screen_and_earlier_ones_by_recency() -> None:
    view = build_view(_read_all(shown_timeline(TIMELINE, PRICING, ROADMAP)).snapshot(), now=NOW)

    assert view.screen_now is not None
    assert (view.screen_now.ref, view.screen_now.shown, view.screen_now.title) == (
        "screen#3",
        "now",
        "Product Roadmap 2027",
    )
    assert [screen.title for screen in view.earlier_screens] == ["Pricing Plans", "Launch Timeline"]
    assert view.earlier_screens[1].figures is not None
    assert view.earlier_screens[1].figures[0].value == "17 March 2027"
    assert view.earlier_screens[0].seconds_ago == 60
    assert view.screen_now.presenter == "Ayu"


def test_a_pending_screen_says_so_and_carries_no_reading() -> None:
    view = build_view(shown_timeline(TIMELINE).snapshot(), now=NOW)

    assert view.screen_now is not None
    assert view.screen_now.reading == "pending"
    assert view.screen_now.title is None


def test_noise_screens_never_show_up_as_earlier_screens() -> None:
    noisy = shown_timeline(TIMELINE, PRICING)
    noisy.read_screen("screen#1", reading("Meet", kind="meeting_ui"))

    assert build_view(noisy.snapshot(), now=NOW).earlier_screens == []


def test_an_unreadable_earlier_screen_is_marked_unavailable() -> None:
    timeline = shown_timeline(TIMELINE, PRICING)
    timeline.read_screen("screen#1", None)

    [earlier] = build_view(timeline.snapshot(), now=NOW).earlier_screens

    assert earlier.reading == "unavailable"


def test_a_later_failed_read_keeps_the_earlier_reading() -> None:
    timeline = _read_all(shown_timeline(TIMELINE, PRICING, ROADMAP))
    timeline.read_screen("screen#2", None)

    assert build_view(timeline.snapshot(), now=NOW).earlier_screens[0].reading == "ready"


def test_chat_signals_and_participants_reach_the_view() -> None:
    timeline = shown_timeline(TIMELINE)
    ayu = timeline.participant("device-ayu", "Ayu")
    budi = timeline.participant("device-budi", "Budi")
    gone = timeline.participant("device-cici", "Cici")
    timeline.add_chat(IncomingChat(message_id="m1", sender=budi, text="deck link?", sent_at=START))
    timeline.add_signal("presenting_started", ayu, START)
    timeline.add_signal("hand_raised", budi, START + timedelta(seconds=30))
    timeline.add_signal("left", gone, START + timedelta(seconds=40))
    timeline.glance_camera(CameraGlance(participant=budi, at=START, description="holds a mockup"))

    view = build_view(timeline.snapshot(), now=NOW)

    assert view.presenting == "Ayu"
    assert [(line.ref, line.sender, line.text) for line in view.chat] == [
        ("chat#1", "Budi", "deck link?")
    ]
    assert [signal.kind for signal in view.signals] == ["presenting_started", "hand_raised", "left"]
    by_name = {person.name: person for person in view.participants}
    assert set(by_name) == {"Ayu", "Budi"}
    assert by_name["Ayu"].presenting
    assert by_name["Budi"].hand_raised
    assert by_name["Budi"].camera == "holds a mockup"


def test_the_rendered_view_is_prefixed_json_without_nulls() -> None:
    text = build_view(shown_timeline(TIMELINE).snapshot(), now=NOW).as_text()

    assert text.startswith(VIEW_PREFIX)
    payload = json.loads(text.removeprefix(VIEW_PREFIX))
    assert "title" not in payload["screen_now"]
    assert payload["screen_now"]["reading"] == "pending"


def test_fitting_drops_detail_from_the_oldest_material_first() -> None:
    timeline = _read_all(shown_timeline(TIMELINE, PRICING, ROADMAP))
    budi = timeline.participant("device-budi", "Budi")
    for index in range(20):
        timeline.add_chat(
            IncomingChat(message_id=f"m{index}", sender=budi, text="x" * 200, sent_at=START)
        )
    full = build_view(timeline.snapshot(), now=NOW)

    fitted = fit_view(full, max_chars=1500)

    assert len(fitted.as_text()) <= 1500
    assert fitted.screen_now is not None
    assert fitted.screen_now.title == "Product Roadmap 2027"
    assert [screen.title for screen in fitted.earlier_screens] == [
        "Pricing Plans",
        "Launch Timeline",
    ]
    assert len(fitted.chat) < len(full.chat)
    assert all(screen.text is None for screen in fitted.earlier_screens)


def test_a_view_that_fits_is_returned_unchanged() -> None:
    view = build_view(shown_timeline(TIMELINE).snapshot(), now=NOW)

    assert fit_view(view, max_chars=10_000) is view
    assert render_view(shown_timeline(TIMELINE).snapshot(), now=NOW, max_chars=10_000) == view


def test_the_current_text_is_trimmed_last() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="word " * 1000))

    fitted = render_view(timeline.snapshot(), now=NOW, max_chars=1200)

    assert len(fitted.as_text()) <= 1200
    assert fitted.screen_now is not None
    assert fitted.screen_now.text is not None
    assert fitted.screen_now.text.endswith("…")


def test_screens_resolve_by_position_reference_or_title() -> None:
    snapshot = _read_all(shown_timeline(TIMELINE, PRICING, ROADMAP)).snapshot()

    def ref(wanted: str) -> str | None:
        found = resolve_screen(snapshot, wanted)
        return found.ref if found else None

    assert ref("current") == "screen#3"
    assert ref("previous") == "screen#2"
    assert ref("screen#1") == "screen#1"
    assert ref("1") == "screen#1"
    assert ref("pricing") == "screen#2"
    assert ref("unknown slide") is None


def test_participants_resolve_by_reference_or_name() -> None:
    timeline = MeetingTimeline()
    timeline.participant("device-ayu", "Ayu Lestari")
    timeline.participant("device-budi", "Budi")
    snapshot = timeline.snapshot()

    assert resolve_participant(snapshot, "person#2") == snapshot.participants[1]
    assert resolve_participant(snapshot, "budi") == snapshot.participants[1]
    assert resolve_participant(snapshot, "ayu") == snapshot.participants[0]
    assert resolve_participant(snapshot, "") is None


def test_a_screen_that_is_not_meeting_content_keeps_its_title_only() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen(
        "screen#1", reading("Cartesia Usage", text="1,105 credits", kind="dashboard")
    )

    view = build_view(timeline.snapshot(), now=NOW)

    assert view.screen_now is not None
    assert view.screen_now.title == "Cartesia Usage"
    assert view.screen_now.text is None
    assert view.screen_now.key_points is None
    assert view.screen_now.figures is None


def test_a_slide_keeps_its_detail() -> None:
    timeline = shown_timeline(TIMELINE)
    timeline.read_screen("screen#1", reading("Launch Timeline", text="Beta 17 March 2027"))

    view = build_view(timeline.snapshot(), now=NOW)

    assert view.screen_now is not None
    assert view.screen_now.text == "Beta 17 March 2027"
