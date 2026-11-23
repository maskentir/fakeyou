import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import pytest
from playwright.async_api import Page, Route

import fakeyou.meet.meet as meet
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import GuestJoinRequest, JoinPolling, JoinResult, request_guest_join
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    ASK_SELECTOR,
    GUEST_MODE_SELECTOR,
    IN_CALL,
    NAME_SELECTOR,
    FakePage,
    meet_page,
)

TWIN = "Observer Twin"
ELSEWHERE = "https://workspace.google.com/products/meet/"
SIZED = "<style>button, input { width: 220px; height: 48px; display: block; margin: 12px; }</style>"
RECORDER = """<script>
window.__effects = [];
const effect = (kind, extra) => window.__effects.push(Object.assign({kind}, extra || {}));
const field = (placeholder, hidden) => {
  const input = document.createElement('input');
  input.placeholder = placeholder;
  input.setAttribute('aria-label', placeholder);
  if (hidden) input.style.display = 'none';
  input.addEventListener('focus', () => effect('name_focus'));
  document.body.appendChild(input);
  return input;
};
const submit = (label, text) => {
  const button = document.createElement('button');
  if (label) button.setAttribute('aria-label', label);
  button.textContent = text;
  button.onclick = () => {
    const typed = [...document.querySelectorAll('input')].map((node) => node.value).filter(Boolean);
    effect('submit', {names: typed});
    document.body.innerHTML = '<div>' + ['Asking', 'to be let in...'].join(' ') + '</div>';
    setTimeout(() => {
      const leave = document.createElement('button');
      leave.setAttribute('aria-label', ['Leave', 'call'].join(' '));
      leave.textContent = 'Leave';
      document.body.replaceChildren(leave);
    }, 300);
  };
  document.body.appendChild(button);
};
const chooser = (disabled) => {
  const button = document.createElement('button');
  button.textContent = 'Join as guest';
  button.disabled = !!disabled;
  button.onclick = () => {
    effect('guest_mode');
    setTimeout(() => {
      document.querySelectorAll('input').forEach((node) => node.remove());
      field('Enter your name', false);
      submit('Ask to join without camera', 'Ask to join');
    }, 400);
  };
  document.body.appendChild(button);
};
</script>"""


@dataclass(frozen=True, slots=True)
class Layout:
    body: str


CLASSIC = Layout(
    "<h1>What's your name?</h1><script>field('Your name', false);"
    "submit('Ask to join without camera', 'Ask to join');</script>"
)
LONG_SUBMIT = Layout(
    "<script>field('Your name', false); submit(null, 'Ask to join without camera');</script>"
)
GUEST_MODE = Layout(
    "<script>field('Enter your name', true);"
    "const sign = document.createElement('button'); sign.textContent = 'Sign in';"
    "document.body.appendChild(sign); chooser(false);</script>"
)
HIDDEN_DUPLICATE = Layout(
    "<script>field('Your name', true); field('Your name', false);"
    "submit('Ask to join without camera', 'Ask to join');</script>"
)
LATE_RENDER = Layout(
    "<h1>Getting ready...</h1><script>setTimeout(() => {"
    "document.body.querySelector('h1').remove(); field('Your name', false);"
    "submit('Ask to join without camera', 'Ask to join'); }, 1500);</script>"
)
VISIBLE_FIELD_AND_CHOOSER = Layout(
    "<script>field('Your name', false); chooser(false);"
    "submit('Ask to join without camera', 'Ask to join');</script>"
)
REFUSED = Layout("<h1>You can’t join this video call</h1><button>Return to home screen</button>")
SIGN_IN_ONLY = Layout("<h1>Sign in to join</h1><button>Sign in</button>")
LEAVES_WHILE_WAITING = Layout(
    f"<h1>Getting ready...</h1><script>setTimeout(() => {{ location.href = '{ELSEWHERE}'; }}, 600);"
    "</script>"
)
DISABLED_CHOOSER = Layout("<script>field('Enter your name', true); chooser(true);</script>")


@asynccontextmanager
async def _meeting(layout: Layout) -> AsyncIterator[Page]:
    page_html = (
        f"<!doctype html><html><head>{SIZED}{RECORDER}</head><body>{layout.body}</body></html>"
    )

    async def serve(route: Route) -> None:
        host = route.request.url.split("/")[2]
        if host == "meet.google.com":
            await route.fulfill(body=page_html, content_type="text/html")
        elif route.request.url.startswith(ELSEWHERE):
            await route.fulfill(body="<h1>Google Meet</h1>", content_type="text/html")
        else:
            await route.abort()

    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.route("**/*", serve)
        yield page


async def _effects(page: Page) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], await page.evaluate("() => window.__effects || []"))


async def _join(page: Page, *, lobby_timeout: float = 5.0) -> JoinResult:
    request = GuestJoinRequest(
        link=LINK,
        twin_name=TWIN,
        lobby_timeout_seconds=lobby_timeout,
        poll_interval_seconds=0.25,
        button_timeout_seconds=3.0,
    )
    async with asyncio.timeout(60):
        return await request_guest_join(page, request, polling=JoinPolling())


def _kinds(effects: list[dict[str, object]]) -> list[object]:
    return [effect["kind"] for effect in effects]


def _submitted_names(effects: list[dict[str, object]]) -> list[object]:
    return [effect["names"] for effect in effects if effect["kind"] == "submit"]


@pytest.fixture(autouse=True)
def _short_name_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meet, "NAME_FIELD_ATTEMPTS", 6)


@pytest.mark.browser
async def test_the_classic_layout_fills_the_name_and_submits_once_without_choosing_a_mode() -> None:
    async with _meeting(CLASSIC) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _kinds(effects).count("guest_mode") == 0
    assert _kinds(effects).count("name_focus") == 1
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_submit_named_ask_to_join_without_camera_knocks_after_the_name() -> None:
    async with _meeting(LONG_SUBMIT) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _kinds(effects) == ["name_focus", "submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_the_guest_mode_layout_chooses_the_mode_then_fills_then_submits_in_order() -> None:
    async with _meeting(GUEST_MODE) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _kinds(effects) == ["guest_mode", "name_focus", "submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
@pytest.mark.parametrize(
    "layout", [HIDDEN_DUPLICATE, LATE_RENDER], ids=["hidden-duplicate", "late"]
)
async def test_a_usable_field_is_found_behind_a_hidden_duplicate_or_a_late_render(
    layout: Layout,
) -> None:
    async with _meeting(layout) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _kinds(effects) == ["name_focus", "submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_visible_field_wins_over_an_offered_guest_mode() -> None:
    async with _meeting(VISIBLE_FIELD_AND_CHOOSER) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert result.status == "admitted"
    assert _kinds(effects) == ["name_focus", "submit"]


@pytest.mark.browser
@pytest.mark.parametrize(
    "layout",
    [REFUSED, SIGN_IN_ONLY, LEAVES_WHILE_WAITING, DISABLED_CHOOSER],
    ids=["refused", "sign-in-only", "destination-moved", "disabled-chooser"],
)
async def test_no_name_is_typed_and_nothing_is_submitted_without_a_usable_form(
    layout: Layout,
) -> None:
    async with _meeting(layout) as page:
        result = await _join(page)
        effects = await _effects(page)
        typed = await page.evaluate(
            "() => [...document.querySelectorAll('input')].map((n) => n.value)"
        )

    assert (result.status, result.knocked) == ("failed", False)
    assert "name_focus" not in _kinds(effects)
    assert "submit" not in _kinds(effects)
    assert "guest_mode" not in _kinds(effects)
    assert TWIN not in typed
    assert TWIN not in result.detail
    assert json.dumps(result.detail).isascii()


@pytest.mark.browser
async def test_a_stop_while_waiting_for_the_form_types_nothing() -> None:
    async with _meeting(LATE_RENDER) as page:
        task = asyncio.create_task(_join(page))
        await asyncio.sleep(0.8)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(1.2)
        effects = await _effects(page)

    assert effects == []


def _fake_request() -> GuestJoinRequest:
    return GuestJoinRequest(
        link=LINK, twin_name=TWIN, lobby_timeout_seconds=4.0, poll_interval_seconds=2.0
    )


async def _fake_join(page: FakePage) -> tuple[JoinResult, FakeClock]:
    clock = FakeClock()
    result = await request_guest_join(
        page, _fake_request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )
    return result, clock


async def test_guest_mode_is_chosen_once_before_the_name_and_the_submit() -> None:
    page = meet_page(
        [IN_CALL],
        counts={GUEST_MODE_SELECTOR: [1]},
        visible={NAME_SELECTOR: False},
        reveal_on_click={GUEST_MODE_SELECTOR: NAME_SELECTOR},
    )

    result, _clock = await _fake_join(page)

    assert result.status == "admitted"
    assert page.clicked == [GUEST_MODE_SELECTOR, NAME_SELECTOR, ASK_SELECTOR]
    assert page.typed == [TWIN]


async def test_a_usable_name_field_is_typed_without_choosing_guest_mode() -> None:
    page = meet_page([IN_CALL], counts={GUEST_MODE_SELECTOR: [1]})

    result, _clock = await _fake_join(page)

    assert result.status == "admitted"
    assert GUEST_MODE_SELECTOR not in page.clicked
    assert page.typed == [TWIN]


async def test_a_late_field_after_guest_mode_never_clicks_the_mode_again() -> None:
    page = meet_page(
        [IN_CALL],
        counts={GUEST_MODE_SELECTOR: [1], NAME_SELECTOR: [0, 0, 0, 1]},
    )

    result, clock = await _fake_join(page)

    assert result.status == "admitted"
    assert page.clicked.count(GUEST_MODE_SELECTOR) == 1
    assert clock.sleeps[:3] == [1.0, 1.0, 1.0]
    assert page.typed == [TWIN]


async def test_a_failed_guest_mode_click_is_not_repeated_and_is_reported_by_type() -> None:
    page = meet_page(
        [],
        counts={GUEST_MODE_SELECTOR: [1], NAME_SELECTOR: [0]},
        click_failures={GUEST_MODE_SELECTOR},
    )

    result, clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert page.clicked == [GUEST_MODE_SELECTOR]
    assert page.typed == []
    assert "guest mode: click failed (Error)" in result.detail
    assert len(clock.sleeps) == meet.NAME_FIELD_ATTEMPTS


async def test_a_disabled_or_hidden_guest_mode_is_never_clicked() -> None:
    for script in ({"disabled": {GUEST_MODE_SELECTOR}}, {"visible": {GUEST_MODE_SELECTOR: False}}):
        page = meet_page([], counts={GUEST_MODE_SELECTOR: [1], NAME_SELECTOR: [0]}, **script)

        result, _clock = await _fake_join(page)

        assert result.status == "failed"
        assert page.clicked == []
        assert "guest mode: not offered" in result.detail


async def test_a_read_only_name_field_is_not_typed_into() -> None:
    page = meet_page([], read_only={NAME_SELECTOR})

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert page.typed == []
    assert ASK_SELECTOR not in page.clicked


async def test_waiting_for_the_form_uses_the_existing_name_budget_once() -> None:
    page = meet_page(
        [IN_CALL],
        counts={NAME_SELECTOR: [0] * 5 + [1]},
        wait_flaky={NAME_SELECTOR: meet.NAME_FIELD_ATTEMPTS},
    )

    result, clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert len(clock.sleeps) == meet.NAME_FIELD_ATTEMPTS
    assert page.typed == []


async def test_a_destination_change_while_waiting_stops_before_any_typing() -> None:
    page = meet_page(
        [], counts={NAME_SELECTOR: [0]}, final_url="https://workspace.google.com/products/meet/"
    )

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert result.detail.startswith("navigation landed on an unexpected host: workspace.google.com")
    assert page.typed == []
