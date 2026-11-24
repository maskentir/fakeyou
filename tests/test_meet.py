import asyncio
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, cast, override

import pytest
from cloakbrowser.human.actionability import ElementNotReceivingEventsError
from playwright.async_api import Error as PlaywrightError

import fakeyou.meet.meet as meet_module
from fakeyou.meet.meet import (
    LOBBY_READ_FAILURE_LIMIT,
    GuestJoinRequest,
    JoinPolling,
    JoinResult,
    call_markers,
    click_when_ready,
    disable_meeting_camera,
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
    with_pinned_meet_locale,
)
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    ASK_SELECTOR,
    CALL_ENDED,
    CAM_OFF_SELECTOR,
    CAM_ON_SELECTOR,
    DENIED,
    DIALOG_SELECTOR,
    ENDED,
    EXPLICIT_DENIED,
    IN_CALL,
    JOIN_NOW_SELECTOR,
    LEAVE_AND_WAITING,
    LEAVE_SELECTOR,
    LIVE_CAPTCHA,
    LOBBY,
    MIC_OFF_SELECTOR,
    MIC_ON_SELECTOR,
    NAME_SELECTOR,
    NO_CAPTCHA,
    REMOVED,
    WAITING,
    FakePage,
    HangingPage,
    meet_page,
)


def _request(**overrides: Any) -> GuestJoinRequest:
    values: dict[str, Any] = {"link": LINK, "twin_name": "Twin"}
    values.update(overrides)
    return GuestJoinRequest(**values)


async def test_request_guest_join_admits_happy_path() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.visited == [f"{LINK}?hl=en"]
    assert page.typed == ["Twin"]
    assert page.clicked == [NAME_SELECTOR, ASK_SELECTOR]


async def test_request_guest_join_fails_when_navigation_lands_on_another_host() -> None:
    page = meet_page([IN_CALL], final_url="https://accounts.google.com/ServiceLogin")
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "accounts.google.com" in result.detail
    assert page.typed == []


async def test_request_guest_join_accepts_join_now_button() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[NO_CAPTCHA] * 8,
        counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [1]},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert JOIN_NOW_SELECTOR in page.clicked


async def test_request_guest_join_reports_denial() -> None:
    page = meet_page([EXPLICIT_DENIED], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "denied"
    assert "denied" in result.detail
    assert len(page.evaluate_calls) == 7
    assert all("recaptcha" in call for call in page.evaluate_calls)


async def test_request_guest_join_reports_platform_block() -> None:
    page = meet_page([DENIED] * 8, evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "blocked"
    assert "blocked" in result.detail
    assert "page says: access refused (unrecognised)" in result.detail


async def test_request_guest_join_retries_after_a_platform_block_and_is_admitted() -> None:
    page = meet_page([DENIED, DENIED, IN_CALL, IN_CALL], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("admitted", True)
    assert page.clicked.count(ASK_SELECTOR) == 2
    assert page.typed == ["Twin", "Twin"]


async def test_request_guest_join_reports_a_meeting_that_ended_in_the_lobby() -> None:
    page = meet_page([ENDED], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "ended"
    assert result.knocked
    assert result.detail == "the meeting ended while the twin was waiting in the lobby"


async def test_access_refusal_with_call_ended_button_does_not_mean_meeting_ended() -> None:
    page = meet_page([CALL_ENDED], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()
    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )
    assert result.status == "blocked"
    assert "page says: access refused (call-ended marker)" in result.detail


async def test_request_guest_join_waits_out_captcha_grace_then_reports_block() -> None:
    page = meet_page([DENIED, DENIED, DENIED], evaluates=[LIVE_CAPTCHA, LIVE_CAPTCHA, LIVE_CAPTCHA])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=4.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "blocked"
    assert "captcha" in result.detail
    assert clock.sleeps == [2.0, 2.0]


async def test_request_guest_join_bounds_a_hung_lobby_poll() -> None:
    page = HangingPage(
        hang_content=True,
        counts={DIALOG_SELECTOR: [0], NAME_SELECTOR: [1], ASK_SELECTOR: [1]},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(poll_interval_seconds=0.01),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "timed out" in result.detail
    assert page.cancelled is True


async def test_request_guest_join_bounds_the_host_probe_after_navigation_failure() -> None:
    page = HangingPage(
        hang_evaluate=True,
        goto_failures=1,
        counts={DIALOG_SELECTOR: [0], NAME_SELECTOR: [1], ASK_SELECTOR: [1]},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(poll_interval_seconds=0.01),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert page.cancelled is True


async def test_request_guest_join_bounds_a_hung_join_attempt() -> None:
    page = HangingPage(
        [DENIED] * 50,
        hang_content=False,
        hang_evaluate=True,
        counts={DIALOG_SELECTOR: [0], NAME_SELECTOR: [1], ASK_SELECTOR: [1]},
    )

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=0.05, poll_interval_seconds=0.01),
    )

    assert result.status == "blocked"
    assert page.cancelled is True


async def test_request_guest_join_proceeds_when_captcha_is_solved() -> None:
    page = meet_page(
        [DENIED, IN_CALL],
        evaluates=[LIVE_CAPTCHA] + [NO_CAPTCHA] * 7 + [LIVE_CAPTCHA] + [NO_CAPTCHA] * 20,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"


async def test_request_guest_join_times_out_in_lobby() -> None:
    page = meet_page([LOBBY, LOBBY, LOBBY, LOBBY, LOBBY], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert "no admission" in result.detail
    assert clock.sleeps == [2.0, 2.0, 2.0]


async def test_request_guest_join_timeout_names_waiting_room() -> None:
    page = meet_page([WAITING, WAITING, WAITING, WAITING, WAITING], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert "still waiting" in result.detail


async def test_request_guest_join_fails_when_lobby_content_keeps_failing() -> None:
    page = meet_page(
        [PlaywrightError("lobby gone")] * LOBBY_READ_FAILURE_LIMIT,
        evaluates=[NO_CAPTCHA] * 8,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("failed", True)
    assert "lobby page content" in result.detail
    assert "(Error)" in result.detail
    assert "lobby gone" not in result.detail


async def test_a_single_unreadable_lobby_poll_keeps_waiting() -> None:
    page = meet_page([PlaywrightError("blip"), WAITING, IN_CALL], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert (result.status, result.knocked) == ("admitted", True)


async def test_the_protocol_admits_before_the_page_is_read() -> None:
    page = meet_page([], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic, admitted=lambda: True),
    )

    assert (result.status, result.detail) == ("admitted", "own device was admitted to the meeting")
    assert page.clicked.count(ASK_SELECTOR) == 1


async def test_a_failure_before_the_knock_is_not_marked_knocked() -> None:
    page = meet_page([], evaluates=[NO_CAPTCHA] * 8, counts={NAME_SELECTOR: [0]})
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert (result.status, result.knocked) == ("failed", False)


async def test_request_guest_join_ignores_leave_marker_while_waiting() -> None:
    page = meet_page([LEAVE_AND_WAITING, EXPLICIT_DENIED], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "denied"


async def test_request_guest_join_aborts_when_stop_requested() -> None:
    page = meet_page(evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic, lambda: True)
    )

    assert result.status == "aborted"


async def test_request_guest_join_accepts_loaded_page_after_navigation_error() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[
            LINK,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
        ],
        goto_failures=1,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.evaluate_calls[0] == "(() => location.href)()"
    assert len(page.evaluate_calls) == 8
    assert all("recaptcha" in call for call in page.evaluate_calls[1:])


async def test_request_guest_join_fails_when_page_is_not_on_meeting_host() -> None:
    page = meet_page(
        [],
        evaluates=[
            "https://example.com/",
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
            NO_CAPTCHA,
        ],
        goto_failures=1,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "navigation" in result.detail


async def test_request_guest_join_fails_without_name_field() -> None:
    page = meet_page([], evaluates=[NO_CAPTCHA] * 8, counts={NAME_SELECTOR: [0]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "name field" in result.detail


async def test_a_missing_name_field_names_the_controls_the_page_showed() -> None:
    page = meet_page(
        [],
        evaluates=[NO_CAPTCHA] * 8,
        counts={NAME_SELECTOR: [0]},
        labels={"button[aria-label]": ["Return to home screen", "Submit feedback"]},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "visible controls: return to home screen, submit feedback" in result.detail


async def test_request_guest_join_waits_out_meet_getting_ready() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 16, wait_flaky={NAME_SELECTOR: 6})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert NAME_SELECTOR in page.waited


async def test_request_guest_join_still_finds_a_late_name_field() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 32, wait_flaky={NAME_SELECTOR: 20})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"


async def test_request_guest_join_fails_without_join_button() -> None:
    page = meet_page(
        [], evaluates=[NO_CAPTCHA] * 8, counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [0]}
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(button_timeout_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "join button" in result.detail


async def test_request_guest_join_waits_for_join_button_to_enable() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[NO_CAPTCHA] * 8,
        counts={ASK_SELECTOR: [0, 1], JOIN_NOW_SELECTOR: [0]},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert clock.sleeps == [1.0]
    assert ASK_SELECTOR in page.clicked


async def test_request_guest_join_dismisses_media_dialog_first() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 8, counts={DIALOG_SELECTOR: [1]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.clicked == [DIALOG_SELECTOR, NAME_SELECTOR, ASK_SELECTOR]


async def test_request_guest_join_fails_when_typing_returns_error() -> None:
    page = meet_page([], evaluates=[NO_CAPTCHA] * 8, type_failures={"Twin"})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "typing the twin name failed" in result.detail


async def test_request_guest_join_fails_when_click_returns_error() -> None:
    page = meet_page([LOBBY], evaluates=[NO_CAPTCHA] * 8, click_failures={ASK_SELECTOR})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("failed", False)
    assert "clicking" in result.detail


async def test_a_knock_that_landed_despite_a_click_error_keeps_waiting() -> None:
    page = meet_page(
        [WAITING, WAITING, IN_CALL], evaluates=[NO_CAPTCHA] * 8, click_failures={ASK_SELECTOR}
    )
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert (result.status, result.knocked) == ("admitted", True)


async def test_an_unreadable_lobby_at_the_deadline_times_out() -> None:
    page = meet_page([WAITING, PlaywrightError("blip")], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    async def sleep(seconds: float) -> None:
        clock.now += seconds

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=2.0, poll_interval_seconds=2.0),
        polling=JoinPolling(sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("timeout", True)


async def test_request_guest_join_retries_transient_name_click() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 16, click_flaky={NAME_SELECTOR: 2})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.clicked.count(NAME_SELECTOR) == 3
    assert page.typed == ["Twin"]


async def test_request_guest_join_gives_up_name_click_after_retries() -> None:
    page = meet_page([], evaluates=[NO_CAPTCHA] * 40, click_failures={NAME_SELECTOR})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "attempts" in result.detail


async def test_request_guest_join_retries_transient_join_click() -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 8, click_flaky={ASK_SELECTOR: 1})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.clicked.count(ASK_SELECTOR) == 2


async def test_request_guest_join_retries_transient_dialog_click() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[NO_CAPTCHA] * 8,
        counts={DIALOG_SELECTOR: [1]},
        click_flaky={DIALOG_SELECTOR: 1},
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.clicked.count(DIALOG_SELECTOR) == 2


async def test_click_when_ready_resolves_locator_once_per_attempt() -> None:
    page = meet_page([])
    clock = FakeClock()
    calls = []

    def locate() -> Any:
        calls.append(1)
        return page.locator(ASK_SELECTOR)

    failure = await click_when_ready(locate, "join button", clock.sleep)

    assert failure is None
    assert calls == [1]


async def test_click_when_ready_resolves_fresh_locator_each_attempt() -> None:
    page = meet_page([], click_flaky={ASK_SELECTOR: 1})
    clock = FakeClock()
    seen = []

    def locate() -> Any:
        locator = page.locator(ASK_SELECTOR)
        seen.append(locator)
        return locator

    failure = await click_when_ready(locate, "join button", clock.sleep)

    assert failure is None
    assert len(seen) == 2
    assert seen[0] is not seen[1]


async def test_click_when_ready_keeps_short_timeouts() -> None:
    calls: dict[str, dict[str, object]] = {}

    class _TimeoutLocator:
        @property
        def first(self) -> "_TimeoutLocator":
            return self

        async def wait_for(self, **kwargs: Any) -> None:
            calls["wait_for"] = kwargs

        async def click(self, **kwargs: Any) -> None:
            calls["click"] = kwargs

    failure = await click_when_ready(cast(Any, _TimeoutLocator), "join button", FakeClock().sleep)

    assert failure is None
    assert calls["wait_for"] == {"state": "visible", "timeout": 2000}
    assert calls["click"] == {"timeout": 5000}


async def test_click_when_ready_reports_error_type_name_only() -> None:
    class _FailingLocator:
        @property
        def first(self) -> "_FailingLocator":
            return self

        async def wait_for(self, **_kwargs: Any) -> None:
            return None

        async def click(self, **_kwargs: Any) -> None:
            raise PlaywrightError("boom secret-token")

    failure = await click_when_ready(
        cast(Any, _FailingLocator), "join button", FakeClock().sleep, 1
    )

    assert failure == "clicking 'join button' failed after 1 attempts: Error"
    assert "secret-token" not in (failure or "")


CHAT_PANEL = (
    '<div role="list" aria-label="Chat messages">'
    "<div>the sprint has ended</div>"
    "<div>you've been removed from the call, just kidding</div>"
    "<div>I left the meeting notes in the doc</div>"
    "<div>Asking to be let in for the review</div>"
    "</div>"
)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (IN_CALL, "in_call"),
        (REMOVED, "removed"),
        (ENDED, "ended"),
        (LOBBY, "lobby"),
        (LEAVE_AND_WAITING, "in_call"),
        (IN_CALL + CHAT_PANEL, "in_call"),
        ("<h1>Your host ended the meeting for everyone</h1>", "ended"),
        ("<h1>The call ended because everyone left</h1>", "ended"),
        (IN_CALL + REMOVED, "removed"),
        (IN_CALL + ENDED, "ended"),
        ("blank page without markers", "unknown"),
    ],
)
async def test_observe_call_state_classifies_page(content: str, expected: str) -> None:
    page = meet_page([content])

    observation = await observe_call_state(page)

    assert observation.state == expected


async def test_leave_meeting_clicks_and_verifies() -> None:
    page = meet_page([IN_CALL, ENDED])
    clock = FakeClock()

    result = await leave_meeting(
        page,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "left"
    assert page.clicked == [LEAVE_SELECTOR]


async def test_leave_meeting_when_already_out_is_left() -> None:
    page = meet_page([ENDED], counts={LEAVE_SELECTOR: [0]})
    clock = FakeClock()

    result = await leave_meeting(
        page,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "left"
    assert page.clicked == []


async def test_leave_meeting_fails_when_leave_button_persists() -> None:
    page = meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL, IN_CALL])
    clock = FakeClock()

    result = await leave_meeting(
        page, sleep=clock.sleep, monotonic=clock.monotonic, timeout_seconds=3.0
    )

    assert result.status == "failed"


async def test_observe_call_state_recognizes_the_post_leave_page() -> None:
    page = meet_page(["<h1>You left the meeting</h1><button>Rejoin</button>"])

    observation = await observe_call_state(page)

    assert observation.state == "left"


async def test_leave_meeting_on_the_post_leave_page_is_left() -> None:
    page = meet_page(
        ["<h1>You left the meeting</h1><button>Rejoin</button>"], counts={LEAVE_SELECTOR: [0]}
    )
    clock = FakeClock()

    result = await leave_meeting(
        page,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "left"
    assert page.clicked == []


async def test_leave_meeting_fails_without_leave_button_on_unknown_page() -> None:
    page = meet_page(["blank page without markers"], counts={LEAVE_SELECTOR: [0]})
    clock = FakeClock()

    result = await leave_meeting(
        page,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "failed"
    assert "leave button not found" in result.detail


def test_with_pinned_meet_locale_adds_bare_language_subtag() -> None:
    assert with_pinned_meet_locale(LINK) == f"{LINK}?hl=en"
    assert with_pinned_meet_locale(f"{LINK}?authuser=1") == f"{LINK}?authuser=1&hl=en"


def test_with_pinned_meet_locale_preserves_existing_hl() -> None:
    link = f"{LINK}?hl=id"
    assert with_pinned_meet_locale(link) == link


def test_with_pinned_meet_locale_keeps_unusable_links() -> None:
    assert with_pinned_meet_locale("http://[::1") == "http://[::1"
    assert with_pinned_meet_locale(LINK, "") == LINK


@pytest.mark.parametrize(
    ("toggle", "current", "opposite", "device"),
    [
        (enable_meeting_microphone, MIC_ON_SELECTOR, MIC_OFF_SELECTOR, "microphone"),
        (disable_meeting_camera, CAM_OFF_SELECTOR, CAM_ON_SELECTOR, "camera"),
    ],
)
@pytest.mark.parametrize(
    "scenario",
    ["already_set", "transient_click", "failed_click", "failed_state", "failed_confirmation"],
)
async def test_media_toggle_preserves_state_and_reports_failure(
    toggle: Callable[..., Coroutine[Any, Any, bool]],
    current: str,
    opposite: str,
    device: str,
    scenario: str,
) -> None:
    values: dict[str, Any] = {}
    if scenario != "already_set":
        values["visible"] = {current: False}
    if scenario == "transient_click":
        values["click_flaky"] = {opposite: 1}
    elif scenario == "failed_click":
        values["click_failures"] = {opposite}
    elif scenario == "failed_state":
        values["visible"] = {current: PlaywrightError("state boom")}
    elif scenario == "failed_confirmation":
        values["wait_failures"] = {current}
    evaluates: list[Any] = []
    if device == "camera":
        evaluates.append(["audioinput", "videoinput", "audiooutput"])
    page = meet_page([], evaluates, **values)
    clock = FakeClock()
    if scenario.startswith("failed"):
        target = "enabled microphone" if device == "microphone" else "disabled camera"
        match = {
            "failed_click": "attempts",
            "failed_state": f"checking {device} state",
            "failed_confirmation": f"waiting for {target}",
        }[scenario]
        with pytest.raises(RuntimeError, match=match):
            await toggle(page, clock.sleep)
    else:
        assert await toggle(page, clock.sleep) is True
        assert page.clicked == ([opposite, opposite] if scenario == "transient_click" else [])


async def test_a_failed_media_toggle_names_the_controls_it_found() -> None:
    page = meet_page(
        [],
        visible={MIC_ON_SELECTOR: False},
        click_failures={MIC_OFF_SELECTOR},
        labels={"button[aria-label]": ["Turn on your microphone", "Leave call"]},
    )
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="visible controls: leave call, microphone toggle$"):
        await enable_meeting_microphone(page, clock.sleep)


async def test_camera_without_a_device_counts_as_disabled_without_waiting() -> None:
    page = meet_page([], visible={CAM_ON_SELECTOR: False, CAM_OFF_SELECTOR: False})
    clock = FakeClock()

    assert await disable_meeting_camera(page, clock.sleep) is True
    assert page.clicked == []
    assert page.waited == []
    assert clock.sleeps == []


async def test_a_host_without_a_camera_device_never_toggles_the_camera() -> None:
    page = meet_page(
        [],
        [["audioinput", "audiooutput"]],
        visible={CAM_ON_SELECTOR: True, CAM_OFF_SELECTOR: False},
    )
    clock = FakeClock()

    assert await disable_meeting_camera(page, clock.sleep) is True
    assert page.clicked == []
    assert page.waited == []


async def test_a_camera_device_is_toggled_off_when_the_host_has_one() -> None:
    page = meet_page(
        [],
        [["audioinput", "videoinput", "audiooutput"]],
        visible={CAM_ON_SELECTOR: True, CAM_OFF_SELECTOR: False},
    )
    clock = FakeClock()

    assert await disable_meeting_camera(page, clock.sleep) is True
    assert page.clicked == [CAM_ON_SELECTOR]


async def test_request_guest_join_admits_on_deadline_recheck() -> None:
    reads: list[int] = []
    page = meet_page(
        [LOBBY, LOBBY, LOBBY, LOBBY, IN_CALL],
        evaluates=[NO_CAPTCHA] * 8,
        on_content=reads.append,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert len(reads) == 5


async def test_request_guest_join_deadline_recheck_failure_keeps_timeout() -> None:
    reads: list[int] = []
    page = meet_page(
        [LOBBY, LOBBY, LOBBY, LOBBY, PlaywrightError("lobby gone")],
        evaluates=[NO_CAPTCHA] * 8,
        on_content=reads.append,
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert len(reads) == 5


async def test_request_guest_join_propagates_unexpected_content_error() -> None:
    page = meet_page([ValueError("bug")], evaluates=[NO_CAPTCHA] * 8)
    clock = FakeClock()

    with pytest.raises(ValueError, match="bug"):
        await request_guest_join(
            page,
            _request(),
            polling=JoinPolling(clock.sleep, clock.monotonic),
        )


async def test_click_when_ready_propagates_unexpected_click_error() -> None:
    class _BuggyLocator:
        @property
        def first(self) -> "_BuggyLocator":
            return self

        async def wait_for(self, **_kwargs: Any) -> None:
            return None

        async def click(self, **_kwargs: Any) -> None:
            raise ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await click_when_ready(cast(Any, _BuggyLocator), "join button", FakeClock().sleep, 1)


async def test_request_guest_join_propagates_unexpected_navigation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = meet_page([IN_CALL])
    clock = FakeClock()

    async def boom(url: str, **_kwargs: Any) -> None:
        del url
        raise ValueError("bug")

    monkeypatch.setattr(page, "goto", boom)
    with pytest.raises(ValueError, match="bug"):
        await request_guest_join(
            page,
            _request(),
            polling=JoinPolling(clock.sleep, clock.monotonic),
        )


async def test_call_state_reads_are_bounded_and_report_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meet_module, "CALL_STATE_TIMEOUT_SECONDS", 0.05)
    page = HangingPage([], hang_content=True)

    observation = await observe_call_state(page)

    assert observation.state == "unknown"


async def test_lobby_preparation_is_inside_the_join_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet_module, "PREPARE_TIMEOUT_SECONDS", 0.05)

    class SlowNavigation(FakePage):
        @override
        async def goto(self, url: str, **kwargs: Any) -> None:
            del kwargs
            self.visited.append(url)
            await asyncio.sleep(5.0)

    page = SlowNavigation()

    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert result.status == "failed"
    assert "lobby preparation did not finish" in result.detail


class _OverlayCoveredLocator:
    def __init__(self, covered_clicks: int) -> None:
        self.covered_clicks = covered_clicks
        self.clicks = 0

    @property
    def first(self) -> "_OverlayCoveredLocator":
        return self

    async def wait_for(self, **options: object) -> None:
        del options

    async def click(self, **options: object) -> None:
        del options
        self.clicks += 1
        if self.clicks <= self.covered_clicks:
            raise ElementNotReceivingEventsError('internal:attr=[placeholder="Your name"i]', "DIV")


async def test_click_when_ready_retries_a_click_blocked_by_an_overlay() -> None:
    locator = _OverlayCoveredLocator(covered_clicks=2)
    clock = FakeClock()

    failure = await click_when_ready(
        lambda: cast(Any, locator), "name field", clock.sleep, attempts=3
    )

    assert failure is None
    assert locator.clicks == 3


async def test_click_when_ready_reports_a_persistent_overlay_instead_of_raising() -> None:
    locator = _OverlayCoveredLocator(covered_clicks=10)
    clock = FakeClock()

    failure = await click_when_ready(
        lambda: cast(Any, locator), "name field", clock.sleep, attempts=2
    )

    assert failure is not None
    assert "name field" in failure
    assert locator.clicks == 2


_PRESENTATION_BANNER_IN_CALL = IN_CALL + '<div role="banner">Presentation</div>'
_CALL_STATE_FIXTURES = {
    "in_call": (IN_CALL, "in_call"),
    "waiting_room": (LOBBY, "lobby"),
    "removed": (REMOVED, "removed"),
    "ended": (ENDED, "ended"),
    "blank": ("<html><body></body></html>", "unknown"),
    "presentation_banner": (_PRESENTATION_BANNER_IN_CALL, "in_call"),
}


@pytest.mark.browser
@pytest.mark.parametrize(
    ("fixture", "expected"),
    [(name, expected) for name, (_, expected) in _CALL_STATE_FIXTURES.items()],
)
async def test_string_match_observer_pins_frozen_call_states(fixture: str, expected: str) -> None:
    from playwright.async_api import async_playwright

    html, _ = _CALL_STATE_FIXTURES[fixture]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            assert (await observe_call_state(page)).state == expected
        finally:
            await browser.close()


async def test_leave_meeting_when_already_out_with_stale_button_is_left() -> None:
    page = meet_page([ENDED], click_failures={LEAVE_SELECTOR})
    clock = FakeClock()

    result = await leave_meeting(
        page,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "left"


@pytest.mark.parametrize(
    ("content", "headings", "buttons"),
    [
        ('<img role="button"><div>Join now</div>', "", frozenset()),
        (
            '<div role="heading"><div>You</div> have been removed</div>',
            "you have been removed",
            frozenset(),
        ),
        (
            '<button aria-label="Leave call"><i>call_end</i></button>'
            "<button><span>Ask</span> <span>to join</span></button>",
            "",
            frozenset({"leave call", "call_end", "ask to join"}),
        ),
        ("<h1>The call <b>has ended", "the call has ended", frozenset()),
    ],
)
def test_call_markers_read_only_headings_and_buttons(
    content: str, headings: str, buttons: frozenset[str]
) -> None:
    assert call_markers(content) == (headings, buttons)


async def _join(page: FakePage, link: str = LINK) -> JoinResult:
    clock = FakeClock()
    return await request_guest_join(
        page, _request(link=link), polling=JoinPolling(clock.sleep, clock.monotonic)
    )


async def test_a_redirect_to_the_meet_home_page_is_not_the_requested_meeting() -> None:
    page = meet_page([IN_CALL], final_url="https://meet.google.com/")

    result = await _join(page)

    assert result.status == "failed"
    assert "Meet home page instead of the requested meeting" in result.detail
    assert "abc-defg-hij" not in result.detail
    assert page.typed == []


async def test_a_redirect_to_another_meeting_is_not_the_requested_meeting() -> None:
    page = meet_page([IN_CALL], final_url="https://meet.google.com/xyz-wxyz-xyz?hl=en")

    result = await _join(page)

    assert result.status == "failed"
    assert "different Meet page instead of the requested meeting" in result.detail
    assert "xyz-wxyz-xyz" not in result.detail
    assert page.typed == []


@pytest.mark.parametrize(
    ("link", "landed"),
    [
        (LINK, f"{LINK}?hl=en"),
        (LINK, f"{LINK}?authuser=0&hl=en&pli=1"),
        (LINK, f"{LINK}/"),
        (f"{LINK}?authuser=1", f"{LINK}?authuser=1&hl=en"),
        ("https://meet.google.com/meet/abc-defg-hij", f"{LINK}?hl=en"),
        (LINK, "https://meet.google.com/ABC-DEFG-HIJ?hl=en"),
    ],
    ids=["pinned_locale", "extra_query", "trailing_slash", "authuser", "meet_prefix", "case"],
)
async def test_a_normalised_meeting_url_is_the_requested_meeting(link: str, landed: str) -> None:
    page = meet_page([IN_CALL], evaluates=[NO_CAPTCHA] * 8, final_url=landed)

    result = await _join(page, link)

    assert result.status == "admitted"


class _RedirectingPage(FakePage):
    def __init__(self, urls: list[str], evaluates: Sequence[Any] = (), **script: Any) -> None:
        super().__init__([], evaluates, **script)
        self.urls = urls

    @property
    @override
    def url(self) -> str:
        return self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]


async def test_a_late_redirect_to_the_home_page_is_a_navigation_failure_not_a_missing_field() -> (
    None
):
    page = _RedirectingPage(
        [f"{LINK}?hl=en", "https://meet.google.com/"],
        counts={NAME_SELECTOR: [0], DIALOG_SELECTOR: [0]},
        labels={"h1, h2": ["Video calls and meetings for everyone"]},
    )

    result = await _join(page)

    assert result.status == "failed"
    assert "Meet home page instead of the requested meeting" in result.detail
    assert "name field" not in result.detail
    assert "heading: Meet home" in result.detail


async def test_a_missing_name_field_on_the_meeting_page_names_its_destination_and_heading() -> None:
    page = meet_page(
        [],
        evaluates=[NO_CAPTCHA] * 8,
        counts={NAME_SELECTOR: [0]},
        labels={"h1, h2": ["Getting ready..."]},
        final_url=f"{LINK}?hl=en",
    )

    result = await _join(page)

    assert result.status == "failed"
    assert "name field" in result.detail
    assert "destination: the requested meeting" in result.detail
    assert "heading: getting ready" in result.detail
    assert "abc-defg-hij" not in result.detail


async def test_a_failed_navigation_never_writes_the_meeting_link() -> None:
    page = meet_page([IN_CALL], ["about:blank"], goto_failures=1)

    result = await _join(page)

    assert result.status == "failed"
    assert "navigation" in result.detail
    assert "abc-defg-hij" not in result.detail


@pytest.mark.parametrize(
    ("landed", "destination"),
    [
        (f"{LINK}?hl=en", "the requested meeting"),
        ("https://meet.google.com/meet/abc-defg-hij", "the requested meeting"),
        ("https://meet.google.com/", "the Meet home page"),
        ("https://meet.google.com", "the Meet home page"),
        ("https://meet.google.com/?hl=en&authuser=0", "the Meet home page"),
        ("https://meet.google.com/landing", "a different Meet page"),
        ("https://meet.google.com/xyz-wxyz-xyz", "a different Meet page"),
        ("https://meet.google.com/abc-defg-hij/extra", "a different Meet page"),
        ("https://accounts.google.com/ServiceLogin", "another origin"),
        ("about:blank", "another origin"),
    ],
)
def test_a_landed_url_is_classified_against_the_requested_meeting(
    landed: str, destination: str
) -> None:
    assert meet_module.landed_destination(landed, LINK) == destination


async def test_a_redirect_to_another_meeting_before_the_name_is_entered_is_never_joined() -> None:
    page = _RedirectingPage(
        [f"{LINK}?hl=en", "https://meet.google.com/xyz-wxyz-xyz?hl=en"],
        counts={NAME_SELECTOR: [1], ASK_SELECTOR: [1], DIALOG_SELECTOR: [0]},
    )

    result = await _join(page)

    assert result.status == "failed"
    assert "a different Meet page instead of the requested meeting" in result.detail
    assert page.typed == []
    assert NAME_SELECTOR not in page.clicked
    assert ASK_SELECTOR not in page.clicked


async def test_a_redirect_after_the_name_is_entered_never_submits_the_join_request() -> None:
    page = _RedirectingPage(
        [f"{LINK}?hl=en", f"{LINK}?hl=en", "https://meet.google.com/xyz-wxyz-xyz?hl=en"],
        evaluates=[NO_CAPTCHA] * 8,
        counts={NAME_SELECTOR: [1], ASK_SELECTOR: [1], DIALOG_SELECTOR: [0]},
    )

    result = await _join(page)

    assert result.status == "failed"
    assert "a different Meet page instead of the requested meeting" in result.detail
    assert ASK_SELECTOR not in page.clicked


@pytest.mark.parametrize(
    "landed",
    [
        "http://meet.google.com/abc-defg-hij",
        "https://meet.google.com:8443/abc-defg-hij",
        "https://meet.google.com.evil.example/abc-defg-hij",
        "javascript:alert(1)",
        "data:text/html,meet.google.com/abc-defg-hij",
    ],
    ids=["http", "port", "lookalike_host", "javascript", "data"],
)
async def test_only_the_expected_meet_origin_is_the_requested_meeting(landed: str) -> None:
    page = meet_page([IN_CALL], final_url=landed)

    result = await _join(page)

    assert meet_module.landed_destination(landed, LINK) == "another origin"
    assert result.status == "failed"
    assert page.typed == []


SENSITIVE_HEADING = (
    "Harun Rasyid 1:1 salary review with ai2@example.com "
    "https://meet.google.com/abc-defg-hij?pli=1&token=s3cr3t"
)
SENSITIVE_PARTS = ("Harun", "salary", "ai2@example.com", "abc-defg-hij", "token", "s3cr3t")


@pytest.mark.parametrize(
    ("landed", "name_fields"),
    [("https://meet.google.com/", 1), (f"{LINK}?hl=en", 0)],
    ids=["wrong_destination", "missing_name_field"],
)
async def test_navigation_evidence_never_repeats_a_sensitive_heading(
    landed: str, name_fields: int
) -> None:
    page = meet_page(
        [],
        evaluates=[NO_CAPTCHA] * 8,
        counts={NAME_SELECTOR: [name_fields]},
        labels={"h1, h2": [SENSITIVE_HEADING, "You can't join this video call"]},
        final_url=landed,
    )

    result = await _join(page)

    assert result.status == "failed"
    assert "heading: unrecognised, access refused" in result.detail
    assert not [part for part in SENSITIVE_PARTS if part in result.detail]


@pytest.mark.parametrize(
    ("heading", "category"),
    [
        ("Video calls and meetings for everyone", "Meet home"),
        ("Ready to join?", "pre-join"),
        ("What's your name?", "pre-join"),
        ("Getting ready...", "getting ready"),
        ("You can't join this video call", "access refused"),
        ("The call has ended", "call ended"),
        ("You left the meeting", "call ended"),
        ("Sign in", "sign-in"),
        ("Check your meeting code", "invalid meeting code"),
        ("Invalid video call name.", "invalid meeting code"),
        ("Weekly sync with Rani", "unrecognised"),
    ],
)
def test_a_heading_is_reported_only_as_a_known_category(heading: str, category: str) -> None:
    assert meet_module.heading_category(heading) == category


OTHER_ROOM = "https://meet.google.com/xyz-wxyz-xyz"
ELSEWHERE_HOME = "https://workspace.google.com/products/meet/"


@pytest.mark.parametrize(
    ("target", "captchas", "flaky", "flip_at", "request_overrides"),
    [
        (OTHER_ROOM, [NO_CAPTCHA] * 16, 1, 1, {}),
        (ELSEWHERE_HOME, [NO_CAPTCHA] * 16, 1, 1, {}),
        (
            OTHER_ROOM,
            [LIVE_CAPTCHA, LIVE_CAPTCHA, NO_CAPTCHA, NO_CAPTCHA, LIVE_CAPTCHA] + [NO_CAPTCHA] * 40,
            0,
            3,
            {"captcha_grace_seconds": 6.0, "poll_interval_seconds": 2.0},
        ),
    ],
    ids=["same-host-room", "other-host", "captcha-grace"],
)
async def test_a_move_while_the_name_field_has_focus_types_nothing(
    target: str, captchas: list[Any], flaky: int, flip_at: int, request_overrides: dict[str, Any]
) -> None:
    page = meet_page(
        [LOBBY],
        evaluates=captchas,
        counts={NAME_SELECTOR: [1]},
        wait_flaky={NAME_SELECTOR: flaky},
    )
    clock = FakeClock()
    moved = False

    async def sleep_then_leave(seconds: float) -> None:
        nonlocal moved
        await clock.sleep(seconds)
        if not moved and len(clock.sleeps) >= flip_at:
            moved = True
            page.visited.append(target)

    result = await request_guest_join(
        page,
        _request(**request_overrides),
        polling=JoinPolling(sleep_then_leave, clock.monotonic),
    )

    assert moved, "the redirect must land while the form is being filled"
    assert page.typed == []
    assert page.clicked == [NAME_SELECTOR]
    assert ASK_SELECTOR not in page.clicked
    assert (result.status, result.knocked) == ("failed", False)
    assert result.detail.startswith("navigation landed on")


async def test_a_redirect_while_the_name_field_is_sought_is_a_navigation_failure() -> None:
    page = _RedirectingPage(
        [f"{LINK}?hl=en", f"{LINK}?hl=en", "https://meet.google.com/"],
        evaluates=[NO_CAPTCHA] * 8,
        counts={NAME_SELECTOR: [0], DIALOG_SELECTOR: [0]},
        labels={"h1, h2": ["Video calls and meetings for everyone"]},
    )

    result = await _join(page)

    assert result.status == "failed"
    assert result.detail == (
        "navigation landed on the Meet home page instead of the requested meeting; "
        "heading: Meet home"
    )
