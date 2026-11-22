from typing import Any

import pytest

from fakeyou.meet.meet import (
    GuestJoinRequest,
    JoinPolling,
    call_markers,
    denial_summary,
    request_guest_join,
)
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    CALL_ENDED,
    EXPLICIT_DENIED,
    LIVE_CAPTCHA,
    LOBBY,
    NO_CAPTCHA,
    FakePageSession,
    meet_page,
)
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import session_config

SENSITIVE = (
    "Rani Wijaya",
    "rani.wijaya@example.com",
    "xqz-mnop-rst",
    "48213-SECRET-7731",
    "jsname",
    "kJU3pb",
    "salary",
)
REFUSAL = "<h1>You can't join this video call</h1>"
SENSITIVE_DENIAL = (
    '<div jsname="UaslIb"></div><h1 jsname="r4nke">You can\'t join this video call</h1>'
    '<p class="kJU3pb">Rani Wijaya\'s 1:1 salary review (xqz-mnop-rst) hosted by '
    "rani.wijaya@example.com, passcode 48213-SECRET-7731</p>"
)


def _leaked(*written: str) -> list[str]:
    text = "\n".join(written)
    return [part for part in SENSITIVE if part in text]


def _request(**overrides: Any) -> GuestJoinRequest:
    values: dict[str, Any] = {"link": LINK, "twin_name": "Twin"}
    values.update(overrides)
    return GuestJoinRequest(**values)


async def test_a_platform_block_reports_a_denial_category_without_page_text() -> None:
    page = meet_page([SENSITIVE_DENIAL], evaluates=[NO_CAPTCHA])
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert result.status == "blocked"
    assert result.detail.startswith("platform blocked the guest; ")
    assert "page says: access refused (unrecognised)" in result.detail
    assert _leaked(result.detail) == []


async def test_an_unsolved_captcha_reports_a_denial_category_without_page_text() -> None:
    page = meet_page([SENSITIVE_DENIAL] * 3, evaluates=[LIVE_CAPTCHA] * 3)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=4.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "blocked"
    assert result.detail.startswith("captcha challenge went unsolved; platform blocked the guest")
    assert "page says: access refused (unrecognised)" in result.detail
    assert _leaked(result.detail) == []
    assert clock.sleeps == [2.0, 2.0]


async def test_a_call_ended_marker_is_reported_as_a_denial_reason() -> None:
    page = meet_page([CALL_ENDED], evaluates=[NO_CAPTCHA])
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert result.status == "blocked"
    assert "page says: access refused (call-ended marker)" in result.detail


async def test_a_host_denial_keeps_its_fixed_detail() -> None:
    page = meet_page([EXPLICIT_DENIED])
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert (result.status, result.detail) == ("denied", "guest request to join was denied")


@pytest.mark.parametrize(
    ("ending", "evaluates", "expected"),
    [
        (SENSITIVE_DENIAL, [NO_CAPTCHA], "blocked_by_platform"),
        (EXPLICIT_DENIED + SENSITIVE_DENIAL, [], "denied_by_host"),
    ],
    ids=["platform_block", "host_denial"],
)
async def test_a_refused_session_never_journals_or_returns_page_text(
    ending: str, evaluates: list[Any], expected: str
) -> None:
    browser = FakePageSession(meet_page([LOBBY, LOBBY, ending], evaluates=evaluates))
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open, sleep=clock.sleep, monotonic=clock.monotonic, journal=journal
        ),
    )

    assert (result.phase, result.terminal_reason) == ("ended", expected)
    assert _leaked(*journal.lines, result.detail) == []
    [finished] = journal.events("join_finished")
    assert result.detail == finished["detail"]


def test_a_reason_counts_only_in_the_refusal_message_not_in_a_comment() -> None:
    message = denial_summary(REFUSAL + "<p>This meeting is full</p>")
    comment = denial_summary(REFUSAL + "<!-- This meeting is full -->")

    assert (message, comment) == ("access refused (meeting full)", "access refused (unrecognised)")


@pytest.mark.parametrize(
    ("content", "summary"),
    [
        ("<h1>You can't join this video call</h1>", "access refused (unrecognised)"),
        ("<h1>You can\u2019t join this video call</h1>", "access refused (unrecognised)"),
        (CALL_ENDED, "access refused (call-ended marker)"),
        (
            "<h1>You can't join this video call</h1><p>No one can join a meeting unless invited"
            " or admitted by the host</p>",
            "access refused (host admission required)",
        ),
        (
            "<h1>You can't join this video call</h1><p>This meeting is full</p>",
            "access refused (meeting full)",
        ),
        (
            "<h1>You can't join this video call</h1><p>Sign in to join this call</p>",
            "access refused (sign-in required)",
        ),
        (
            REFUSAL + "<p>Only people in your organization can join this call</p>",
            "access refused (organization restricted)",
        ),
        (REFUSAL + "<p>Only people in Rani Wijaya's team</p>", "access refused (unrecognised)"),
        (
            REFUSAL + "<script>const note = 'This meeting is full';</script>",
            "access refused (unrecognised)",
        ),
        (REFUSAL + "<style>/* This meeting is full */</style>", "access refused (unrecognised)"),
        (
            REFUSAL + "<template><p>This meeting is full</p></template>",
            "access refused (unrecognised)",
        ),
        (
            REFUSAL + "<div hidden><section><p>This meeting is full</p></section></div>",
            "access refused (unrecognised)",
        ),
        (
            "<html><head><title>This meeting is full</title></head><body>"
            + REFUSAL
            + "</body></html>",
            "access refused (unrecognised)",
        ),
        (
            REFUSAL
            + "<div hidden><p>Sign in to join this call</p></div><p>This meeting is full</p>",
            "access refused (meeting full)",
        ),
        (REFUSAL + '<div title="This meeting is full"></div>', "access refused (unrecognised)"),
        (REFUSAL + '<div aria-label="Sign in to join"></div>', "access refused (unrecognised)"),
        (
            "<div data-call-ended='true'></div>" + REFUSAL,
            "access refused (call-ended marker)",
        ),
        ('<!-- data-call-ended="true" -->' + REFUSAL, "access refused (unrecognised)"),
        ('<div data-call-ended="false"></div>' + REFUSAL, "access refused (unrecognised)"),
        (
            "<p>This meeting is full</p>" + CALL_ENDED,
            "access refused (call-ended marker, meeting full)",
        ),
        (
            REFUSAL + "<p>Sign in to join this call</p><p>This meeting is full</p>",
            "access refused (meeting full, sign-in required)",
        ),
        (
            REFUSAL + "<p>This meeting is <b>full</b></p>",
            "access refused (meeting full)",
        ),
        (
            CALL_ENDED + "<p>This meeting is full</p>",
            "access refused (call-ended marker, meeting full)",
        ),
        (SENSITIVE_DENIAL, "access refused (unrecognised)"),
        ("<p>Rani Wijaya can't join this right now</p>", "none (unrecognised)"),
        ("<h1>Rani Wijaya's meeting xqz-mnop-rst</h1>", "unrecognised (unrecognised)"),
        ("", "none (unrecognised)"),
    ],
)
def test_denial_pages_map_to_fixed_categories(content: str, summary: str) -> None:
    assert denial_summary(content) == summary


def test_call_markers_still_read_headings_and_buttons_inside_concealed_subtrees() -> None:
    content = (
        "<head><title>Meet</title></head><div hidden><h1>Getting ready</h1></div>"
        '<template><button aria-label="Leave call"></button></template>'
    )

    assert call_markers(content) == ("getting ready", frozenset({"leave call"}))
