import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route

import fakeyou.meet.meet as meet
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import GuestJoinRequest, JoinPolling, JoinResult, request_guest_join
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    ASK_SELECTOR,
    ASK_WITHOUT_CAMERA_SELECTOR,
    GUEST_MODE_SELECTOR,
    IN_CALL,
    JOIN_NOW_SELECTOR,
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
document.addEventListener('click', (event) => {
  const button = event.target.closest('button');
  if (button) effect('click', {id: button.id || button.textContent.trim()});
}, true);
const helper = (id, text) => {
  const button = document.createElement('button');
  button.id = id;
  button.textContent = text;
  document.body.appendChild(button);
};
const field = (placeholder, hidden) => {
  const input = document.createElement('input');
  input.placeholder = placeholder;
  input.setAttribute('aria-label', placeholder);
  if (hidden) input.style.display = 'none';
  input.addEventListener('focus', () => effect('name_focus'));
  document.body.appendChild(input);
  return input;
};
const arm = (button) => {
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
};
const submit = (label, text, hidden) => {
  const button = document.createElement('button');
  button.id = 'submit';
  if (hidden) button.style.display = 'none';
  if (label) button.setAttribute('aria-label', label);
  button.textContent = text;
  arm(button);
  document.body.appendChild(button);
};
const cover = (target, onHover) => {
  const rect = target.getBoundingClientRect();
  const shield = document.createElement('div');
  shield.id = 'shield';
  Object.assign(shield.style, {position: 'absolute', left: rect.left + 'px', top: rect.top + 'px',
    width: rect.width + 'px', height: rect.height + 'px', zIndex: 10});
  let fired = false;
  shield.addEventListener('pointermove', () => {
    if (fired) return;
    fired = true;
    onHover();
    setTimeout(() => shield.remove(), 900);
  });
  document.body.appendChild(shield);
};
const chooser = (disabled) => {
  const button = document.createElement('button');
  button.id = 'chooser';
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
HELP_BEFORE_CHOOSER = Layout(
    "<script>field('Enter your name', true); helper('help', 'Join as guest help');"
    "chooser(false);</script>"
)
HELP_BEFORE_SUBMIT = Layout(
    "<script>field('Your name', false); helper('help', 'Ask to join instructions');"
    "submit(null, 'Ask to join without camera');</script>"
)
HELP_ONLY_SUBMIT = Layout(
    "<script>field('Your name', false); helper('help', 'Ask to join instructions');</script>"
)
HIDDEN_ASK_VISIBLE_VARIANT = Layout(
    "<script>field('Your name', false); helper('stale', 'Ask to join');"
    "document.getElementById('stale').style.display = 'none';"
    "submit(null, 'Ask to join without camera');</script>"
)
RETRY_HELP = Layout(
    "<script>field('Your name', false); submit(null, 'Ask to join');"
    "cover(document.getElementById('submit'), () => {"
    "const help = document.createElement('button'); help.id = 'help';"
    "help.textContent = 'Ask to join instructions'; document.body.prepend(help); });</script>"
)
RETRY_GONE = Layout(
    "<script>field('Your name', false); submit(null, 'Ask to join');"
    "cover(document.getElementById('submit'), () => {"
    "document.getElementById('submit').remove();"
    "const help = document.createElement('button'); help.id = 'help';"
    "help.textContent = 'Ask to join instructions'; document.body.prepend(help); });</script>"
)
LABELLED_SUBMIT = Layout(
    "<span id='help-name'>Ask to join instructions</span>"
    "<button id='help' aria-labelledby='help-name'>Ask to join</button>"
    "<span id='submit-name'>Ask to join</span>"
    "<button id='submit' aria-labelledby='submit-name'><svg width='20' height='20'></svg></button>"
    "<script>field('Your name', false); arm(document.getElementById('submit'));</script>"
)
LABELLED_CHOOSER = Layout(
    "<span id='help-name'>Join as guest help</span>"
    "<button id='help' aria-labelledby='help-name'>Join as guest</button>"
    "<span id='chooser-name'>Join as guest</span>"
    "<script>field('Enter your name', true); chooser(false);"
    "const official = document.getElementById('chooser'); official.textContent = '';"
    "official.setAttribute('aria-labelledby', 'chooser-name');</script>"
)
SPLIT_SUBMIT = Layout(
    "<span id='ask-a'>Ask to</span>"
    "<span id='ask-b'>join</span>"
    "<span id='help-name'>Ask to join instructions</span>"
    "<button id='help' aria-labelledby='help-name'>Ask to join</button>"
    "<button id='submit' aria-labelledby='ask-a ask-b'>"
    "<svg width='20' height='20'></svg></button>"
    "<script>field('Your name', false); arm(document.getElementById('submit'));</script>"
)
HELP_RACE = Layout(
    "<script>field('Your name', false);"
    "const official = document.createElement('button'); official.id = 'submit';"
    "official.textContent = 'Ask to join'; arm(official);"
    "document.body.appendChild(official);"
    "official.addEventListener('pointermove', () => {"
    "if (document.getElementById('help')) return;"
    "const help = document.createElement('button'); help.id = 'help';"
    "help.textContent = 'Ask to join instructions';"
    "official.remove(); document.body.prepend(help);"
    "setTimeout(() => document.body.appendChild(official), 400);"
    "});</script>"
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
            await route.fulfill(body=page_html, content_type="text/html; charset=utf-8")
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
    return [effect["kind"] for effect in effects if effect["kind"] != "click"]


def _clicked(effects: list[dict[str, object]]) -> list[object]:
    return [effect["id"] for effect in effects if effect["kind"] == "click"]


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
async def test_a_help_control_before_the_official_guest_chooser_is_never_clicked() -> None:
    async with _meeting(HELP_BEFORE_CHOOSER) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == ["chooser", "submit"]
    assert _kinds(effects) == ["guest_mode", "name_focus", "submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
@pytest.mark.parametrize(
    "layout", [HELP_BEFORE_SUBMIT, HIDDEN_ASK_VISIBLE_VARIANT], ids=["help-first", "hidden-ask"]
)
async def test_only_the_visible_official_submit_is_clicked_once(layout: Layout) -> None:
    async with _meeting(layout) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == ["submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_retry_reselects_the_official_submit_after_a_help_control_appears() -> None:
    async with _meeting(RETRY_HELP) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == ["submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_submit_gone_during_the_retry_is_a_failure_before_the_knock() -> None:
    async with _meeting(RETRY_GONE) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert _clicked(effects) == []
    assert "submit" not in _kinds(effects)


@pytest.mark.browser
@pytest.mark.parametrize(
    ("layout", "clicked"),
    [(LABELLED_SUBMIT, ["submit"]), (LABELLED_CHOOSER, ["chooser", "submit"])],
    ids=["submit", "chooser"],
)
async def test_aria_labelledby_names_decide_which_control_is_clicked(
    layout: Layout, clicked: list[str]
) -> None:
    async with _meeting(layout) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == clicked
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_submit_named_across_two_labelledby_refs_is_clicked_once() -> None:
    async with _meeting(SPLIT_SUBMIT) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == ["submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_help_inserted_mid_click_never_receives_the_official_submit_click() -> None:
    async with _meeting(HELP_RACE) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert _clicked(effects) == ["submit"]
    assert _submitted_names(effects) == [[TWIN]]


@pytest.mark.browser
async def test_a_help_control_alone_is_not_a_knock() -> None:
    async with _meeting(HELP_ONLY_SUBMIT) as page:
        result = await _join(page)
        effects = await _effects(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert _clicked(effects) == []
    assert "submit" not in _kinds(effects)


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


async def test_a_control_whose_name_only_starts_like_the_chooser_is_never_clicked() -> None:
    page = meet_page(
        [],
        counts={GUEST_MODE_SELECTOR: [1], NAME_SELECTOR: [0]},
        names={GUEST_MODE_SELECTOR: "Join as guest help"},
    )

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert page.clicked == []
    assert "guest mode: not offered" in result.detail


async def test_a_hidden_configured_submit_falls_through_to_the_visible_variant() -> None:
    page = meet_page(
        [IN_CALL],
        counts={ASK_WITHOUT_CAMERA_SELECTOR: [1]},
        visible={ASK_SELECTOR: False},
    )

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert page.clicked == [NAME_SELECTOR, ASK_WITHOUT_CAMERA_SELECTOR]


async def test_a_submit_whose_name_only_starts_like_ask_to_join_is_not_a_knock() -> None:
    page = meet_page(
        [],
        counts={JOIN_NOW_SELECTOR: [0]},
        names={ASK_SELECTOR: "Ask to join instructions"},
    )

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert ASK_SELECTOR not in page.clicked
    assert result.detail == "join button did not become available on the lobby page"


async def test_a_rebuilt_nth_locator_clicks_the_inserted_help_button() -> None:
    page = meet_page([], nodes={ASK_SELECTOR: ["Ask to join"]})
    candidate = page.locator(ASK_SELECTOR).nth(0)

    assert await candidate.evaluate(meet.ACCESSIBLE_NAME_PROBE) == "Ask to join"

    help_key = page.dom_insert(ASK_SELECTOR, 0, "Ask to join instructions")
    await candidate.click()

    assert page.clicked == [ASK_SELECTOR]
    assert page.clicked_nodes == [(ASK_SELECTOR, help_key)]


async def test_the_verified_handle_clicks_the_official_submit_after_a_help_insert() -> None:
    page = meet_page([], nodes={ASK_SELECTOR: ["Ask to join"]})
    official_key = page.nodes[ASK_SELECTOR][0].key
    handle = await page.locator(ASK_SELECTOR).nth(0).element_handle()

    assert handle is not None
    assert await handle.evaluate(meet.ACCESSIBLE_NAME_PROBE) == "Ask to join"

    page.dom_insert(ASK_SELECTOR, 0, "Ask to join instructions")

    assert await handle.is_visible()
    assert await handle.is_enabled()
    await handle.click(timeout=5000)

    assert page.clicked == [ASK_SELECTOR]
    assert page.clicked_nodes == [(ASK_SELECTOR, official_key)]


async def test_a_detached_handle_click_fails_instead_of_clicking_help() -> None:
    page = meet_page([], nodes={ASK_SELECTOR: ["Ask to join"]})
    official_key = page.nodes[ASK_SELECTOR][0].key
    handle = await page.locator(ASK_SELECTOR).nth(0).element_handle()

    assert handle is not None

    page.dom_remove(ASK_SELECTOR, official_key)
    page.dom_insert(ASK_SELECTOR, 0, "Ask to join instructions")

    assert not await handle.is_visible()
    with pytest.raises(PlaywrightError):
        await handle.click(timeout=5000)

    assert page.clicked == []
    assert page.clicked_nodes == []


async def test_only_a_help_submit_in_the_dom_is_not_a_knock() -> None:
    page = meet_page(
        [], counts={JOIN_NOW_SELECTOR: [0]}, nodes={ASK_SELECTOR: ["Ask to join instructions"]}
    )

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert ASK_SELECTOR not in page.clicked
    assert page.clicked_nodes == [(NAME_SELECTOR, "")]
    assert result.detail == "join button did not become available on the lobby page"


async def test_a_dom_modelled_official_submit_knocks_once() -> None:
    page = meet_page([IN_CALL], nodes={ASK_SELECTOR: ["Ask to join"]})
    official_key = page.nodes[ASK_SELECTOR][0].key

    result, _clock = await _fake_join(page)

    assert (result.status, result.knocked) == ("admitted", True)
    assert page.clicked == [NAME_SELECTOR, ASK_SELECTOR]
    assert page.clicked_nodes == [(NAME_SELECTOR, ""), (ASK_SELECTOR, official_key)]
    assert page.typed == [TWIN]


async def test_guest_mode_through_dom_nodes_chooses_once_before_the_submit() -> None:
    page = meet_page(
        [IN_CALL],
        nodes={GUEST_MODE_SELECTOR: ["Join as guest"]},
        visible={NAME_SELECTOR: False},
        reveal_on_click={GUEST_MODE_SELECTOR: NAME_SELECTOR},
    )
    guest_key = page.nodes[GUEST_MODE_SELECTOR][0].key

    result, _clock = await _fake_join(page)

    assert result.status == "admitted"
    assert page.clicked == [GUEST_MODE_SELECTOR, NAME_SELECTOR, ASK_SELECTOR]
    assert page.clicked_nodes[0] == (GUEST_MODE_SELECTOR, guest_key)
    assert page.typed == [TWIN]


def test_control_selector_covers_every_labelledby_word() -> None:
    selector = meet.control_selector("Ask to join")

    assert selector.startswith("xpath=")
    for position in range(1, 5):
        assert f"id(@aria-labelledby)[{position}]" in selector
    for word in ("ask", "to", "join"):
        assert selector.count(f"'{word}'") == 6
