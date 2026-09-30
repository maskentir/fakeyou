import pytest

from fakeyou.meet import (
    GuestJoinRequest,
    _click_when_ready,
    disable_meeting_camera,
    enable_meeting_microphone,
    leave_meeting,
    observe_call_state,
    request_guest_join,
    with_pinned_meet_locale,
)

LINK = "https://meet.google.com/abc-defg-hij"

LOBBY = '<input placeholder="Your name"><button>Ask to join</button>'
JOIN_NOW_LOBBY = '<input placeholder="Your name"><button>Join now</button>'
DIALOG_LOBBY = (
    '<input placeholder="Your name">'
    "<div>Do you want people to see and hear you in the meeting?</div>"
    "<button>Continue without microphone and camera</button>"
)
IN_CALL = '<button aria-label="Leave call"></button><button>Mute microphone</button>'
DENIED = "<h1>You can't join this video call</h1>"
EXPLICIT_DENIED = "<h1>Someone in the call denied your request to join</h1>"
REMOVED = "<h1>You've been removed from the call</h1>"
ENDED = "<h1>The call has ended</h1>"
WAITING = LOBBY + "<div>Asking to be let in...</div>"
LEAVE_AND_WAITING = IN_CALL + "<div>Asking to be let in...</div>"
LOBBY_WITHOUT_BUTTON = '<input placeholder="Your name">'

DIALOG_SELECTOR = 'text="Continue without microphone and camera"'
NAME_SELECTOR = "placeholder:Your name"
ASK_SELECTOR = 'text="Ask to join"'
JOIN_NOW_SELECTOR = 'text="Join now"'
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'
MIC_ON_SELECTOR = (
    'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
)
MIC_OFF_SELECTOR = (
    'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
)
CAM_ON_SELECTOR = 'button[aria-label^="Turn off camera" i]'
CAM_OFF_SELECTOR = 'button[aria-label^="Turn on camera" i]'

NO_CAPTCHA = False
LIVE_CAPTCHA = True


class _FakeKeyboard:
    def __init__(self, page):
        self._page = page

    async def type(self, text, delay=0):
        self._page.typed.append(text)
        if text in self._page.type_failures:
            raise RuntimeError(f"typing failed: {text}")


class _FakeLocator:
    def __init__(self, page, selector):
        self._page = page
        self.selector = selector

    @property
    def first(self):
        return self

    async def count(self):
        scripted = self._page.counts.get(self.selector, [1])
        if len(scripted) > 1:
            return scripted.pop(0)
        return scripted[0]

    async def wait_for(self, **kwargs):
        if self.selector in self._page.wait_failures:
            raise RuntimeError(f"wait failed: {self.selector}")
        self._page.waited.append(self.selector)

    async def is_visible(self):
        value = self._page.visible.get(self.selector, True)
        if isinstance(value, Exception):
            raise value
        return value

    async def click(self, **kwargs):
        self._page.clicked.append(self.selector)
        remaining = self._page.click_flaky.get(self.selector, 0)
        if remaining > 0:
            self._page.click_flaky[self.selector] = remaining - 1
            raise RuntimeError(f"click failed: {self.selector}")
        if self.selector in self._page.click_failures:
            raise RuntimeError(f"click failed: {self.selector}")


class _FakePage:
    def __init__(
        self,
        contents=(),
        evaluates=(),
        counts=None,
        click_failures=(),
        click_flaky=None,
        type_failures=(),
        goto_failures=0,
        visible=None,
        wait_failures=(),
    ):
        self._contents = list(contents)
        self._evaluates = list(evaluates)
        self.counts = {key: list(value) for key, value in (counts or {}).items()}
        self.click_failures = set(click_failures)
        self.click_flaky = dict(click_flaky or {})
        self.visible = dict(visible or {})
        self.wait_failures = set(wait_failures)
        self.type_failures = set(type_failures)
        self._goto_failures = goto_failures
        self.visited = []
        self.clicked = []
        self.typed = []
        self.waited = []
        self.evaluate_calls = []
        self.keyboard = _FakeKeyboard(self)

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        if self._goto_failures > 0:
            self._goto_failures -= 1
            raise RuntimeError("navigation timeout")
        return None

    async def content(self):
        if not self._contents:
            raise AssertionError("page content read more times than scripted")
        result = self._contents.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def evaluate(self, expression):
        self.evaluate_calls.append(expression)
        if not self._evaluates:
            raise AssertionError("page evaluate called more times than scripted")
        result = self._evaluates.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def get_by_placeholder(self, text):
        return _FakeLocator(self, f"placeholder:{text}")


class _FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _request(**overrides):
    values = {"link": LINK, "twin_name": "Twin"}
    values.update(overrides)
    return GuestJoinRequest(**values)


def _page(contents=(), **overrides):
    counts = {
        DIALOG_SELECTOR: [0],
        NAME_SELECTOR: [1],
        ASK_SELECTOR: [1],
    }
    counts.update(overrides.pop("counts", {}))
    return _FakePage(contents, counts=counts, **overrides)


async def test_request_guest_join_admits_happy_path() -> None:
    page = _page([IN_CALL])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.visited == [f"{LINK}?hl=en"]
    assert page.typed == ["Twin"]
    assert page.clicked == [NAME_SELECTOR, ASK_SELECTOR]


async def test_request_guest_join_accepts_join_now_button() -> None:
    page = _page([IN_CALL], counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [1]})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert JOIN_NOW_SELECTOR in page.clicked


async def test_request_guest_join_reports_denial() -> None:
    page = _page([EXPLICIT_DENIED])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "denied"
    assert "denied" in result.detail
    assert page.evaluate_calls == []


async def test_request_guest_join_reports_platform_block() -> None:
    page = _page([DENIED], evaluates=[NO_CAPTCHA])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "blocked"
    assert "blocked" in result.detail


async def test_request_guest_join_waits_out_captcha_grace_then_reports_block() -> None:
    page = _page([DENIED, DENIED, DENIED], evaluates=[LIVE_CAPTCHA, LIVE_CAPTCHA, LIVE_CAPTCHA])
    clock = _FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=4.0, poll_interval_seconds=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "blocked"
    assert "captcha" in result.detail
    assert clock.sleeps == [2.0, 2.0]


async def test_request_guest_join_proceeds_when_captcha_is_solved() -> None:
    page = _page([DENIED, IN_CALL], evaluates=[LIVE_CAPTCHA])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"


async def test_request_guest_join_times_out_in_lobby() -> None:
    page = _page([LOBBY, LOBBY, LOBBY, LOBBY])
    clock = _FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "timeout"
    assert "no admission" in result.detail
    assert clock.sleeps == [2.0, 2.0, 2.0]


async def test_request_guest_join_timeout_names_waiting_room() -> None:
    page = _page([WAITING, WAITING, WAITING, WAITING])
    clock = _FakeClock()

    result = await request_guest_join(
        page,
        _request(lobby_timeout_seconds=5.0, poll_interval_seconds=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "timeout"
    assert "still waiting" in result.detail


async def test_request_guest_join_fails_when_lobby_content_read_fails() -> None:
    page = _page([RuntimeError("lobby gone")])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "lobby page content" in result.detail


async def test_request_guest_join_ignores_leave_marker_while_waiting() -> None:
    page = _page([LEAVE_AND_WAITING, EXPLICIT_DENIED])
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "denied"


async def test_request_guest_join_aborts_when_stop_requested() -> None:
    page = _page()
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic, abort=lambda: True
    )

    assert result.status == "aborted"


async def test_request_guest_join_accepts_loaded_page_after_navigation_error() -> None:
    page = _page([IN_CALL], evaluates=[LINK], goto_failures=1)
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.evaluate_calls == ["(() => location.href)()"]


async def test_request_guest_join_fails_when_page_is_not_on_meeting_host() -> None:
    page = _page([], evaluates=["https://example.com/"], goto_failures=1)
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "navigation" in result.detail


async def test_request_guest_join_fails_without_name_field() -> None:
    page = _page([], counts={NAME_SELECTOR: [0]})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "name field" in result.detail


async def test_request_guest_join_fails_without_join_button() -> None:
    page = _page([], counts={ASK_SELECTOR: [0], JOIN_NOW_SELECTOR: [0]})
    clock = _FakeClock()

    result = await request_guest_join(
        page,
        _request(button_timeout_seconds=2.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.status == "failed"
    assert "join button" in result.detail


async def test_request_guest_join_waits_for_join_button_to_enable() -> None:
    page = _page([IN_CALL], counts={ASK_SELECTOR: [0, 1], JOIN_NOW_SELECTOR: [0]})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert clock.sleeps == [1.0]
    assert ASK_SELECTOR in page.clicked


async def test_request_guest_join_dismisses_media_dialog_first() -> None:
    page = _page([IN_CALL], counts={DIALOG_SELECTOR: [1]})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.clicked == [DIALOG_SELECTOR, NAME_SELECTOR, ASK_SELECTOR]


async def test_request_guest_join_fails_when_typing_returns_error() -> None:
    page = _page([], type_failures={"Twin"})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "typing the twin name failed" in result.detail


async def test_request_guest_join_fails_when_click_returns_error() -> None:
    page = _page([], click_failures={ASK_SELECTOR})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "clicking" in result.detail


async def test_request_guest_join_retries_transient_name_click() -> None:
    page = _page([IN_CALL], click_flaky={NAME_SELECTOR: 2})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.clicked.count(NAME_SELECTOR) == 3
    assert page.typed == ["Twin"]


async def test_request_guest_join_gives_up_name_click_after_retries() -> None:
    page = _page([], click_failures={NAME_SELECTOR})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "failed"
    assert "attempts" in result.detail


async def test_request_guest_join_retries_transient_join_click() -> None:
    page = _page([IN_CALL], click_flaky={ASK_SELECTOR: 1})
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.clicked.count(ASK_SELECTOR) == 2


async def test_request_guest_join_retries_transient_dialog_click() -> None:
    page = _page(
        [IN_CALL],
        counts={DIALOG_SELECTOR: [1]},
        click_flaky={DIALOG_SELECTOR: 1},
    )
    clock = _FakeClock()

    result = await request_guest_join(
        page, _request(), sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result.status == "admitted"
    assert page.clicked.count(DIALOG_SELECTOR) == 2


async def test_click_when_ready_resolves_locator_once_per_attempt() -> None:
    page = _page([])
    clock = _FakeClock()
    calls = []

    def locate():
        calls.append(1)
        return page.locator(ASK_SELECTOR)

    failure = await _click_when_ready(locate, "join button", clock.sleep)

    assert failure is None
    assert calls == [1]


async def test_click_when_ready_resolves_fresh_locator_each_attempt() -> None:
    page = _page([], click_flaky={ASK_SELECTOR: 1})
    clock = _FakeClock()
    seen = []

    def locate():
        locator = page.locator(ASK_SELECTOR)
        seen.append(locator)
        return locator

    failure = await _click_when_ready(locate, "join button", clock.sleep)

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

    failure = await _click_when_ready(_TimeoutLocator, "join button", _FakeClock().sleep)

    assert failure is None
    assert calls["wait_for"] == {"state": "visible", "timeout": 2000}
    assert calls["click"] == {"timeout": 5000}


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
    page = _page([content])

    observation = await observe_call_state(page)

    assert observation.state == expected


async def test_leave_meeting_clicks_and_verifies() -> None:
    page = _page([IN_CALL, ENDED])
    clock = _FakeClock()

    result = await leave_meeting(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.status == "left"
    assert page.clicked == [LEAVE_SELECTOR]


async def test_leave_meeting_when_already_out_is_left() -> None:
    page = _page([ENDED], counts={LEAVE_SELECTOR: [0]})
    clock = _FakeClock()

    result = await leave_meeting(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.status == "left"
    assert page.clicked == []


async def test_leave_meeting_fails_when_leave_button_persists() -> None:
    page = _page([IN_CALL, IN_CALL, IN_CALL, IN_CALL])
    clock = _FakeClock()

    result = await leave_meeting(
        page, sleep=clock.sleep, monotonic=clock.monotonic, timeout_seconds=3.0
    )

    assert result.status == "failed"


async def test_leave_meeting_fails_without_leave_button_on_unknown_page() -> None:
    page = _page(["blank page without markers"], counts={LEAVE_SELECTOR: [0]})
    clock = _FakeClock()

    result = await leave_meeting(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.status == "failed"
    assert "leave button not found" in result.detail


async def test_enable_meeting_microphone_skips_click_when_already_on() -> None:
    page = _page([])
    clock = _FakeClock()

    assert await enable_meeting_microphone(page, clock.sleep) is True
    assert page.clicked == []


async def test_enable_meeting_microphone_retries_transient_click() -> None:
    page = _page(
        [],
        visible={MIC_ON_SELECTOR: False},
        click_flaky={MIC_OFF_SELECTOR: 1},
    )
    clock = _FakeClock()

    assert await enable_meeting_microphone(page, clock.sleep) is True
    assert page.clicked.count(MIC_OFF_SELECTOR) == 2


async def test_enable_meeting_microphone_raises_after_retries() -> None:
    page = _page([], visible={MIC_ON_SELECTOR: False}, click_failures={MIC_OFF_SELECTOR})
    clock = _FakeClock()

    with pytest.raises(RuntimeError, match="attempts"):
        await enable_meeting_microphone(page, clock.sleep)


async def test_enable_meeting_microphone_raises_context_when_state_check_fails() -> None:
    page = _page([], visible={MIC_ON_SELECTOR: RuntimeError("state boom")})
    clock = _FakeClock()

    with pytest.raises(RuntimeError, match="checking microphone state"):
        await enable_meeting_microphone(page, clock.sleep)


async def test_enable_meeting_microphone_raises_context_when_confirm_wait_fails() -> None:
    page = _page([], visible={MIC_ON_SELECTOR: False}, wait_failures={MIC_ON_SELECTOR})
    clock = _FakeClock()

    with pytest.raises(RuntimeError, match="waiting for enabled microphone"):
        await enable_meeting_microphone(page, clock.sleep)


async def test_disable_meeting_camera_skips_click_when_already_off() -> None:
    page = _page([])
    clock = _FakeClock()

    assert await disable_meeting_camera(page, clock.sleep) is True
    assert page.clicked == []


async def test_disable_meeting_camera_retries_transient_click() -> None:
    page = _page(
        [],
        visible={CAM_OFF_SELECTOR: False},
        click_flaky={CAM_ON_SELECTOR: 1},
    )
    clock = _FakeClock()

    assert await disable_meeting_camera(page, clock.sleep) is True
    assert page.clicked.count(CAM_ON_SELECTOR) == 2


async def test_disable_meeting_camera_raises_after_retries() -> None:
    page = _page([], visible={CAM_OFF_SELECTOR: False}, click_failures={CAM_ON_SELECTOR})
    clock = _FakeClock()

    with pytest.raises(RuntimeError, match="attempts"):
        await disable_meeting_camera(page, clock.sleep)


async def test_disable_meeting_camera_raises_context_when_state_check_fails() -> None:
    page = _page([], visible={CAM_OFF_SELECTOR: RuntimeError("state boom")})
    clock = _FakeClock()

    with pytest.raises(RuntimeError, match="checking camera state"):
        await disable_meeting_camera(page, clock.sleep)


def test_with_pinned_meet_locale_adds_bare_language_subtag() -> None:
    assert with_pinned_meet_locale(LINK) == f"{LINK}?hl=en"
    assert with_pinned_meet_locale(f"{LINK}?authuser=1") == f"{LINK}?authuser=1&hl=en"


def test_with_pinned_meet_locale_preserves_existing_hl() -> None:
    link = f"{LINK}?hl=id"
    assert with_pinned_meet_locale(link) == link


def test_with_pinned_meet_locale_keeps_unusable_links() -> None:
    assert with_pinned_meet_locale("http://[::1") == "http://[::1"
    assert with_pinned_meet_locale(LINK, "") == LINK
