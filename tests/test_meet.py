import pytest

from fakeyou.meet.meet import (
    GuestJoinRequest,
    JoinPolling,
    click_when_ready,
    disable_meeting_camera,
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
    with_pinned_meet_locale,
)
from tests.fakes import (
    ASK_SELECTOR,
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
    LINK,
    LIVE_CAPTCHA,
    LOBBY,
    MIC_OFF_SELECTOR,
    MIC_ON_SELECTOR,
    NAME_SELECTOR,
    NO_CAPTCHA,
    REMOVED,
    WAITING,
    FakeClock,
    meet_page,
)


def _request(**overrides):
    values = {"link": LINK, "twin_name": "Twin"}
    values.update(overrides)
    return GuestJoinRequest(**values)


async def test_request_guest_join_admits_happy_path() -> None:
    page = meet_page([IN_CALL])
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


async def test_request_guest_join_accepts_join_now_button() -> None:
    page = meet_page([IN_CALL], counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [1]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert JOIN_NOW_SELECTOR in page.clicked


async def test_request_guest_join_reports_denial() -> None:
    page = meet_page([EXPLICIT_DENIED])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "denied"
    assert "denied" in result.detail
    assert page.evaluate_calls == []


async def test_request_guest_join_reports_platform_block() -> None:
    page = meet_page([DENIED], evaluates=[NO_CAPTCHA])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "blocked"
    assert "blocked" in result.detail


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


async def test_request_guest_join_proceeds_when_captcha_is_solved() -> None:
    page = meet_page([DENIED, IN_CALL], evaluates=[LIVE_CAPTCHA])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"


async def test_request_guest_join_times_out_in_lobby() -> None:
    page = meet_page([LOBBY, LOBBY, LOBBY, LOBBY])
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
    page = meet_page([WAITING, WAITING, WAITING, WAITING])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert "still waiting" in result.detail


async def test_request_guest_join_fails_when_lobby_content_read_fails() -> None:
    page = meet_page([RuntimeError("lobby gone")])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "lobby page content" in result.detail
    assert "(RuntimeError)" in result.detail
    assert "lobby gone" not in result.detail


async def test_request_guest_join_ignores_leave_marker_while_waiting() -> None:
    page = meet_page([LEAVE_AND_WAITING, EXPLICIT_DENIED])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "denied"


async def test_request_guest_join_aborts_when_stop_requested() -> None:
    page = meet_page()
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic, lambda: True)
    )

    assert result.status == "aborted"


async def test_request_guest_join_accepts_loaded_page_after_navigation_error() -> None:
    page = meet_page([IN_CALL], evaluates=[LINK], goto_failures=1)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.evaluate_calls == ["(() => location.href)()"]


async def test_request_guest_join_fails_when_page_is_not_on_meeting_host() -> None:
    page = meet_page([], evaluates=["https://example.com/"], goto_failures=1)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "navigation" in result.detail


async def test_request_guest_join_fails_without_name_field() -> None:
    page = meet_page([], counts={NAME_SELECTOR: [0]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "name field" in result.detail


async def test_request_guest_join_waits_out_meet_getting_ready() -> None:
    page = meet_page([IN_CALL], wait_flaky={NAME_SELECTOR: 6})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert NAME_SELECTOR in page.waited


async def test_request_guest_join_fails_without_join_button() -> None:
    page = meet_page([], counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [0]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(button_timeout_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "join button" in result.detail


async def test_request_guest_join_waits_for_join_button_to_enable() -> None:
    page = meet_page([IN_CALL], counts={ASK_SELECTOR: [0, 1], JOIN_NOW_SELECTOR: [0]})
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
    page = meet_page([IN_CALL], counts={DIALOG_SELECTOR: [1]})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "admitted"
    assert page.clicked == [DIALOG_SELECTOR, NAME_SELECTOR, ASK_SELECTOR]


async def test_request_guest_join_fails_when_typing_returns_error() -> None:
    page = meet_page([], type_failures={"Twin"})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "typing the twin name failed" in result.detail


async def test_request_guest_join_fails_when_click_returns_error() -> None:
    page = meet_page([], click_failures={ASK_SELECTOR})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "clicking" in result.detail


async def test_request_guest_join_retries_transient_name_click() -> None:
    page = meet_page([IN_CALL], click_flaky={NAME_SELECTOR: 2})
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
    page = meet_page([], click_failures={NAME_SELECTOR})
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "failed"
    assert "attempts" in result.detail


async def test_request_guest_join_retries_transient_join_click() -> None:
    page = meet_page([IN_CALL], click_flaky={ASK_SELECTOR: 1})
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

    def locate():
        calls.append(1)
        return page.locator(ASK_SELECTOR)

    failure = await click_when_ready(locate, "join button", clock.sleep)

    assert failure is None
    assert calls == [1]


async def test_click_when_ready_resolves_fresh_locator_each_attempt() -> None:
    page = meet_page([], click_flaky={ASK_SELECTOR: 1})
    clock = FakeClock()
    seen = []

    def locate():
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
        def first(self):
            return self

        async def wait_for(self, **kwargs):
            calls["wait_for"] = kwargs

        async def click(self, **kwargs):
            calls["click"] = kwargs

    failure = await click_when_ready(_TimeoutLocator, "join button", FakeClock().sleep)

    assert failure is None
    assert calls["wait_for"] == {"state": "visible", "timeout": 2000}
    assert calls["click"] == {"timeout": 5000}


async def test_click_when_ready_reports_error_type_name_only() -> None:
    class _FailingLocator:
        @property
        def first(self):
            return self

        async def wait_for(self, **_kwargs):
            return None

        async def click(self, **_kwargs):
            raise RuntimeError("boom secret-token")

    failure = await click_when_ready(_FailingLocator, "join button", FakeClock().sleep, 1)

    assert failure == "clicking 'join button' failed after 1 attempts: RuntimeError"
    assert "secret-token" not in (failure or "")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (IN_CALL, "in_call"),
        (REMOVED, "removed"),
        (ENDED, "ended"),
        (LOBBY, "lobby"),
        (LEAVE_AND_WAITING, "lobby"),
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
    page = meet_page([IN_CALL, IN_CALL, IN_CALL, IN_CALL])
    clock = FakeClock()

    result = await leave_meeting(
        page, sleep=clock.sleep, monotonic=clock.monotonic, timeout_seconds=3.0
    )

    assert result.status == "failed"


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
    toggle, current, opposite, device, scenario
):
    values = {}
    if scenario != "already_set":
        values["visible"] = {current: False}
    if scenario == "transient_click":
        values["click_flaky"] = {opposite: 1}
    elif scenario == "failed_click":
        values["click_failures"] = {opposite}
    elif scenario == "failed_state":
        values["visible"] = {current: RuntimeError("state boom")}
    elif scenario == "failed_confirmation":
        values["wait_failures"] = {current}
    page = meet_page([], **values)
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


async def test_request_guest_join_admits_on_deadline_recheck() -> None:
    reads: list[int] = []
    page = meet_page([LOBBY, LOBBY, LOBBY, LOBBY, IN_CALL], on_content=reads.append)
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
        [LOBBY, LOBBY, LOBBY, LOBBY, RuntimeError("lobby gone")], on_content=reads.append
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert result.status == "timeout"
    assert len(reads) == 5
