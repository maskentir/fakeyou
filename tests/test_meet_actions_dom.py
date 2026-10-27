import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, Self, cast

import pytest
from playwright.async_api import Page

import fakeyou.meet.meet_actions as meet_actions_module
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet_actions import (
    MeetActionSignals,
    lower_hand,
    observe_chat_echoed,
    observe_hand_lowered,
    observe_hand_raised,
    raise_hand,
    send_chat,
)
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import IN_CALL, FakePage, meet_page
from tests._meet_support import (
    CHAT_BUTTON_XPATH,
    CHAT_INPUT_XPATH,
    CHAT_TEXT,
    ECHO_CONTENT,
    FIXTURE_HTML,
    FIXTURE_URL,
    LOWER_XPATH,
    RAISE_XPATH,
    button_xpath,
    with_enter,
)

ECHO_ITEM_XPATH = button_xpath("pin message")
TEXT_WITHOUT_PIN = IN_CALL + f"<div>{CHAT_TEXT}</div>"


async def test_raise_hand_noop_when_already_raised() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: True})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert "already" in result.detail
    assert page.clicked == []


async def test_raise_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH]


async def test_lower_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={RAISE_XPATH: False})
    clock = FakeClock()

    result = await lower_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [LOWER_XPATH]


async def test_raise_hand_status_timeout_raises() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, wait_failures={LOWER_XPATH})
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="raised"):
        await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == [RAISE_XPATH]


async def test_hand_click_retries_three_times_with_fresh_locator() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, click_flaky={RAISE_XPATH: 2})
    clock = FakeClock()
    locate_calls: list[str] = []
    real_locator = page.locator

    def counting(selector: str) -> Any:
        locate_calls.append(selector)
        return real_locator(selector)

    page_any = cast(Any, page)
    page_any.locator = counting

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH, RAISE_XPATH, RAISE_XPATH]
    assert locate_calls.count(RAISE_XPATH) == 3


async def test_send_chat_opens_panel_types_and_succeeds_on_echo() -> None:
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [CHAT_BUTTON_XPATH, CHAT_INPUT_XPATH]
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    assert CHAT_TEXT not in result.detail


async def test_send_chat_fails_without_echo_and_never_resends() -> None:
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    clock = FakeClock()
    pressed = with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert "not observed" in result.detail
    assert CHAT_TEXT not in result.detail
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]


async def test_send_chat_fails_when_pin_item_missing() -> None:
    page = meet_page([IN_CALL] + [TEXT_WITHOUT_PIN] * 11, counts={ECHO_ITEM_XPATH: [0] * 11})
    clock = FakeClock()
    with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert page.typed == [CHAT_TEXT]


async def test_send_chat_rejects_empty_text_without_touching_dom() -> None:
    page = meet_page([])
    clock = FakeClock()

    with pytest.raises(ValueError, match="empty"):
        await send_chat(page, "   ", sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == []
    assert page.typed == []


async def test_custom_signals_drive_selectors() -> None:
    signals = MeetActionSignals(raise_hand="lever la main", lower_hand="baisser la main")
    page = meet_page([IN_CALL], visible={button_xpath("baisser la main"): False})
    clock = FakeClock()

    result = await raise_hand(page, signals, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [button_xpath("lever la main")]


@pytest.mark.browser
async def test_hand_and_chat_actions_against_fixture_page() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.route(
            FIXTURE_URL,
            lambda route: route.fulfill(body=FIXTURE_HTML, content_type="text/html"),
        )
        await real_page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        raised = await raise_hand(real_page)
        assert raised.succeeded
        lowered = await lower_hand(real_page)
        assert lowered.succeeded
        sent = await send_chat(real_page, "live fixture ping")
        assert sent.succeeded


class _ExplodingLocator:
    def __init__(self, real: Any, *, method: str) -> None:
        self._real = real
        self._method = method

    @property
    def first(self) -> Self:
        return self

    async def count(self) -> int:
        if self._method == "count":
            raise KeyError("locator bug")
        return cast(int, await self._real.count())

    async def wait_for(self, **kwargs: Any) -> None:
        if self._method == "wait_for":
            raise KeyError("locator bug")
        await self._real.wait_for(**kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _explode_locator(page: FakePage, method: str, selector: str | None = None) -> None:
    real_locator = page.locator

    def exploding(target: str, **kwargs: Any) -> Any:
        real = real_locator(target, **kwargs)
        if selector is not None and target != selector:
            return real
        return _ExplodingLocator(real, method=method)

    page_any = cast(Any, page)
    page_any.locator = exploding


async def test_send_chat_echo_count_bug_propagates() -> None:
    page = meet_page([IN_CALL] + [ECHO_CONTENT] * 12, visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    with_enter(page)
    _explode_locator(page, "count")

    with pytest.raises(KeyError, match="locator bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_send_chat_panel_wait_bug_propagates() -> None:
    page = meet_page([IN_CALL], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    _explode_locator(page, "wait_for", CHAT_INPUT_XPATH)

    with pytest.raises(KeyError, match="locator bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_send_chat_typing_bug_propagates() -> None:
    page = meet_page([IN_CALL])
    clock = FakeClock()
    with_enter(page)
    page_any = cast(Any, page)

    async def boom(text: str, **kwargs: Any) -> None:
        del kwargs
        del text
        raise KeyError("keyboard bug")

    page_any.keyboard.type = boom

    with pytest.raises(KeyError, match="keyboard bug"):
        await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)


async def test_echo_wait_bounds_each_content_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet_actions_module, "_ECHO_READ_TIMEOUT_SECONDS", 0.01)
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    reads = 0

    async def content() -> str:
        nonlocal reads
        reads += 1
        if reads == 1:
            return IN_CALL
        await asyncio.sleep(5.0)
        return IN_CALL

    monkeypatch.setattr(page, "content", content)
    with_enter(page)
    clock = FakeClock()
    started = time.monotonic()

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert time.monotonic() - started < 2.0


_RAISED_HAND_PAGE = '<button aria-label="Lower hand"></button>'
_CHAT_ECHO_PAGE = '<div>hello everyone</div><button aria-label="Pin message"></button>'


async def _with_static_page(html: str, check: Callable[[Page], Awaitable[None]]) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html)
            await check(page)
        finally:
            await browser.close()


@pytest.mark.browser
async def test_hand_readback_sees_raised_hand_only_when_lower_control_is_visible() -> None:
    async def check(page: Page) -> None:
        assert await observe_hand_raised(page) is True
        await page.set_content("<html><body></body></html>")
        assert await observe_hand_raised(page) is False

    await _with_static_page(_RAISED_HAND_PAGE, check)


@pytest.mark.browser
async def test_hand_lowered_readback_sees_raise_control() -> None:
    async def check(page: Page) -> None:
        assert await observe_hand_lowered(page) is True
        await page.set_content(_RAISED_HAND_PAGE)
        assert await observe_hand_lowered(page) is False

    await _with_static_page('<button aria-label="Raise hand"></button>', check)


@pytest.mark.browser
async def test_chat_readback_needs_text_and_echo_item() -> None:
    async def check(page: Page) -> None:
        assert await observe_chat_echoed(page, "hello everyone") is True
        assert await observe_chat_echoed(page, "not sent yet") is False

    await _with_static_page(_CHAT_ECHO_PAGE, check)


MIC_ON_XPATH = button_xpath("turn on microphone")
MIC_OFF_XPATH = button_xpath("turn off microphone")
REACT_XPATH = button_xpath("send a reaction")
LEAVE_SELECTOR = 'button[aria-label="Leave call"]'


async def test_mute_clicks_the_microphone_toggle() -> None:
    from fakeyou.meet.meet_actions import mute_microphone

    page = meet_page([IN_CALL], visible={MIC_ON_XPATH: False})
    clock = FakeClock()

    result = await mute_microphone(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [MIC_OFF_XPATH]


async def test_mute_is_a_noop_when_already_muted() -> None:
    from fakeyou.meet.meet_actions import mute_microphone

    page = meet_page([IN_CALL], visible={MIC_ON_XPATH: True})
    clock = FakeClock()

    result = await mute_microphone(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert "already" in result.detail
    assert page.clicked == []


async def test_unmute_clicks_the_microphone_toggle() -> None:
    from fakeyou.meet.meet_actions import unmute_microphone

    page = meet_page([IN_CALL], visible={MIC_OFF_XPATH: False})
    clock = FakeClock()

    result = await unmute_microphone(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [MIC_ON_XPATH]


async def test_react_opens_the_picker_and_clicks_the_emoji() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    emoji = "\U0001f44d"
    page = meet_page([IN_CALL], evaluates=[False, [emoji]])
    clock = FakeClock()

    result = await send_reaction(page, emoji, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [REACT_XPATH, f'button[aria-label="{emoji}"]']


async def test_leave_clicks_the_leave_button() -> None:
    from fakeyou.meet.meet_actions import leave_call

    page = meet_page([IN_CALL])
    clock = FakeClock()

    result = await leave_call(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [LEAVE_SELECTOR]


async def test_react_falls_back_to_a_dom_search_when_selectors_miss() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    emoji = "\U0001f44d"
    page = meet_page(
        [IN_CALL],
        click_failures={f'button:has-text("{emoji}")', f'button[aria-label*="{emoji}"]'},
        evaluates=[False, True],
    )
    clock = FakeClock()

    result = await send_reaction(page, emoji, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked[0] == REACT_XPATH
    assert page.evaluate_calls


async def test_react_skips_the_toggle_when_the_picker_is_already_open() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    emoji = "\U0001f44d"
    page = meet_page([IN_CALL], evaluates=[True, [emoji]])
    clock = FakeClock()

    result = await send_reaction(page, emoji, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [f'button[aria-label="{emoji}"]']


async def test_react_maps_a_plain_heart_to_the_meet_heart() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    page = meet_page([IN_CALL], evaluates=[False, ["\U0001f496"]])
    clock = FakeClock()

    result = await send_reaction(page, "\u2764\ufe0f", sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert 'button[aria-label="\U0001f496"]' in page.clicked


async def test_react_maps_a_confused_face_to_the_meet_thinking_face() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    page = meet_page([IN_CALL], evaluates=[False, ["\U0001f914"]])
    clock = FakeClock()

    result = await send_reaction(page, "\U0001f615", sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert 'button[aria-label="\U0001f914"]' in page.clicked


async def test_react_fails_fast_when_meet_does_not_offer_the_emoji() -> None:
    from fakeyou.meet.meet_actions import send_reaction

    page = meet_page([IN_CALL], evaluates=[False, ["\U0001f44d", "\U0001f496"]])
    clock = FakeClock()

    result = await send_reaction(page, "\U0001f984", sleep=clock.sleep, monotonic=clock.monotonic)

    assert not result.succeeded
    assert "does not offer" in result.detail
    assert page.clicked == [REACT_XPATH]
