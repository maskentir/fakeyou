import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from playwright.async_api import Page, Route

import fakeyou.meet.meet as meet
from fakeyou.config.settings import MeetingSettings
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import (
    VISIBLE_BODY_SELECTOR,
    VISIBLE_CONTROLS_SELECTOR,
    GuestJoinRequest,
    JoinPolling,
    MeetSignals,
    request_guest_join,
)
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    ASK_SELECTOR,
    EXPLICIT_DENIED,
    IN_CALL,
    LIVE_CAPTCHA,
    LOBBY,
    NAME_SELECTOR,
    NO_CAPTCHA,
    WAITING,
    FakePageSession,
    meet_page,
)
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import session_config

SPELLINGS = ("can't", "can’t", "can&rsquo;t")
SPELLING_IDS = ("ascii", "curly", "entity")
LEAK = ("Rani Wijaya", "rani.wijaya@example.com", "xqz-mnop-rst")
PRIVATE = "<p>Rani Wijaya's review (xqz-mnop-rst) hosted by rani.wijaya@example.com</p>"
HOME = "<button>Return to home screen</button><button>Submit feedback</button>"


def _heading(spelling: str) -> str:
    return f"<h1>You {spelling} join this video call</h1>" + PRIVATE + HOME


def _plain(spelling: str) -> str:
    return f"<div>You {spelling} join this video call</div>" + PRIVATE + HOME


def _request(**overrides: Any) -> GuestJoinRequest:
    values: dict[str, Any] = {
        "link": LINK,
        "twin_name": "Twin",
        "lobby_timeout_seconds": 6.0,
        "poll_interval_seconds": 2.0,
    }
    values.update(overrides)
    return GuestJoinRequest(**values)


def _leaked(*written: str) -> list[str]:
    text = "\n".join(written)
    return [part for part in LEAK if part in text]


async def _join(
    page: Any, request: GuestJoinRequest | None = None, signals: MeetSignals | None = None
) -> Any:
    clock = FakeClock()
    return await request_guest_join(
        page,
        request or _request(),
        signals or MeetSignals(),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )


@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
@pytest.mark.parametrize("layout", [_heading, _plain], ids=["heading", "plain"])
async def test_a_refusal_after_the_knock_blocks_for_every_apostrophe(
    spelling: str, layout: Any
) -> None:
    page = meet_page([layout(spelling)], evaluates=[NO_CAPTCHA])

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", True)
    assert result.detail.startswith("platform blocked the guest; page says: ")
    assert _leaked(result.detail) == []


@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
async def test_a_refusal_after_the_knock_ends_the_session_blocked_by_platform(
    spelling: str,
) -> None:
    page = meet_page([LOBBY, LOBBY, _heading(spelling)], evaluates=[NO_CAPTCHA])
    browser = FakePageSession(page)
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(meeting=MeetingSettings(join_max_attempts=3)),
        SessionWiring(
            open_page=browser.open, sleep=clock.sleep, monotonic=clock.monotonic, journal=journal
        ),
    )

    assert (result.phase, result.terminal_reason) == ("ended", "blocked_by_platform")
    attempts = journal.events("join_attempt_finished")
    assert [(row["attempt"], row["status"]) for row in attempts] == [(1, "blocked")]
    assert page.clicked.count(ASK_SELECTOR) == 1
    assert _leaked(*journal.lines, result.detail) == []


@pytest.mark.parametrize(
    "content",
    [
        WAITING + '<script>const note = "You can\'t join this video call";</script>',
        WAITING + "<!-- You can't join this video call -->",
        WAITING + '<div title="You can\'t join this video call"></div>',
        WAITING + "<template><h1>You can't join this video call</h1></template>",
        WAITING + "<div hidden><h1>You can't join this video call</h1></div>",
        WAITING + '<div aria-hidden="true"><h1>You can\'t join this video call</h1></div>',
        WAITING + '<h1 style="display: none">You can\'t join this video call</h1>',
        WAITING + "<p>Tip: if it says you can't join this video call, ask the host</p>",
    ],
    ids=[
        "script",
        "comment",
        "attribute",
        "template",
        "hidden",
        "aria-hidden",
        "css",
        "lobby-quote",
    ],
)
async def test_incidental_refusal_text_on_a_normal_page_never_blocks(content: str) -> None:
    page = meet_page([content] * 8, evaluates=[NO_CAPTCHA] * 8)

    result = await _join(page)

    assert result.status == "timeout"


async def test_a_quoted_refusal_in_the_call_is_still_admitted() -> None:
    page = meet_page([IN_CALL + "<p>Someone wrote: you can’t join this video call?</p>"])

    result = await _join(page)

    assert result.status == "admitted"


async def test_a_host_denial_keeps_priority_over_a_generic_refusal() -> None:
    page = meet_page([EXPLICIT_DENIED + _heading("can’t")])

    result = await _join(page)

    assert (result.status, result.detail) == ("denied", "guest request to join was denied")


async def test_a_live_captcha_keeps_its_grace_before_a_curly_refusal_blocks() -> None:
    page = meet_page([_heading("can’t")] * 3, evaluates=[LIVE_CAPTCHA] * 3)

    result = await _join(page, _request(captcha_grace_seconds=4.0))

    assert result.status == "blocked"
    assert result.detail.startswith("captcha challenge went unsolved")


async def test_configured_signals_decide_what_a_refusal_is() -> None:
    page = meet_page(["<h1>Access is not possible here</h1>" + HOME], evaluates=[NO_CAPTCHA])
    default_page = meet_page([_heading("can’t")] * 8, evaluates=[NO_CAPTCHA] * 8)

    custom = await _join(page, signals=MeetSignals(denied="access is not possible"))
    ignored = await _join(default_page, signals=MeetSignals(denied="access is not possible"))

    assert custom.status == "blocked"
    assert ignored.status == "timeout"


def _refusal_before_the_knock(spelling: str, **script: Any) -> Any:
    message = f"You {spelling.replace('&rsquo;', chr(0x2019))} join this video call"
    labels = {
        VISIBLE_BODY_SELECTOR: [f"{message}\nReturn to home screen\nSubmit feedback"],
        VISIBLE_CONTROLS_SELECTOR: ["Return to home screen", "Submit feedback"],
    }
    labels.update(script.pop("labels", {}))
    return meet_page(
        [_heading(spelling)],
        evaluates=[NO_CAPTCHA],
        counts={NAME_SELECTOR: [1]},
        wait_flaky={NAME_SELECTOR: 100},
        labels=labels,
        **script,
    )


@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
async def test_a_refusal_before_the_knock_blocks_without_typing_or_submitting(
    spelling: str,
) -> None:
    page = _refusal_before_the_knock(spelling)

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", False)
    assert result.detail.startswith("platform refused the guest before the knock; page says: ")
    assert page.typed == []
    assert ASK_SELECTOR not in page.clicked
    assert _leaked(result.detail) == []


async def test_a_refusal_before_the_knock_ends_blocked_by_platform_without_a_retry() -> None:
    page = _refusal_before_the_knock("can’t")
    browser = FakePageSession(page)
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(meeting=MeetingSettings(join_max_attempts=3)),
        SessionWiring(
            open_page=browser.open, sleep=clock.sleep, monotonic=clock.monotonic, journal=journal
        ),
    )

    assert (result.phase, result.terminal_reason) == ("ended", "blocked_by_platform")
    attempts = journal.events("join_attempt_finished")
    assert [(row["attempt"], row["status"]) for row in attempts] == [(1, "blocked")]
    assert len(page.visited) == 1
    assert page.typed == []
    assert ASK_SELECTOR not in page.clicked
    assert _leaked(*journal.lines, result.detail) == []


async def test_a_quoted_refusal_beside_lobby_controls_before_the_knock_is_not_a_refusal() -> None:
    page = _refusal_before_the_knock(
        "can’t",
        labels={VISIBLE_CONTROLS_SELECTOR: ["Ask to join", "Turn off microphone"]},
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert result.detail.startswith("clicking 'name field' failed")


async def test_a_redirect_before_the_knock_keeps_the_destination_guard() -> None:
    page = meet_page(
        [],
        counts={NAME_SELECTOR: [0]},
        final_url="https://workspace.google.com/products/meet/",
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("failed", False)
    assert result.detail.startswith("navigation landed on an unexpected host: workspace.google.com")


SIZED = "<style>button, input { width: 220px; height: 48px; display: block; margin: 12px; }</style>"
ANNOUNCE = "You {spelling} join this video call"


def _browser_refusal(spelling: str, tag: str) -> str:
    message = ANNOUNCE.format(spelling=spelling)
    return (
        "<input placeholder='Your name' style='display:none'><h1>Getting ready...</h1>"
        "<script>setTimeout(() => { document.body.innerHTML = "
        f"`<{tag}>{message}</{tag}>` + "
        "'<button>Return to home screen</button><button>Submit feedback</button>'; }, 700);"
        "</script>"
    )


def _browser_after_knock(spelling: str) -> str:
    message = ANNOUNCE.format(spelling=spelling)
    return (
        "<input placeholder='Your name'><button id='ask'>Ask to join</button><script>"
        "document.querySelector('#ask').onclick = () => { setTimeout(() => {"
        f"document.body.innerHTML = `<h1>{message}</h1><button>Return to home screen</button>`;"
        "}, 300); };</script>"
    )


def _browser_hidden_quote() -> str:
    return (
        "<input placeholder='Your name'><button id='ask'>Ask to join</button>"
        "<h1 style='display:none'>You can’t join this video call</h1>"
    )


@asynccontextmanager
async def _browser_meeting(body: str) -> AsyncIterator[Page]:
    html = f"<!doctype html><html><head>{SIZED}</head><body>{body}</body></html>"

    async def serve(route: Route) -> None:
        if route.request.url.split("/")[2] == "meet.google.com":
            await route.fulfill(body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort()

    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.route("**/*", serve)
        yield page


async def _browser_join(page: Page) -> Any:
    request = _request(lobby_timeout_seconds=3.0, poll_interval_seconds=0.5)
    async with asyncio.timeout(90):
        return await request_guest_join(page, request, polling=JoinPolling())


@pytest.mark.browser
@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
@pytest.mark.parametrize("tag", ["h1", "div"])
async def test_a_real_refusal_page_before_the_knock_is_blocked_without_typing(
    spelling: str, tag: str
) -> None:
    async with _browser_meeting(_browser_refusal(spelling, tag)) as page:
        result = await _browser_join(page)
        typed = await page.evaluate(
            "() => [...document.querySelectorAll('input')].map((n) => n.value)"
        )

    assert (result.status, result.knocked) == ("blocked", False)
    assert result.detail.startswith("platform refused the guest before the knock; page says: ")
    assert "Twin" not in typed


@pytest.mark.browser
@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
async def test_a_real_refusal_after_the_knock_is_blocked(spelling: str) -> None:
    async with _browser_meeting(_browser_after_knock(spelling)) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == ("blocked", True)


@pytest.mark.browser
async def test_a_css_hidden_refusal_on_a_real_lobby_is_not_a_refusal() -> None:
    async with _browser_meeting(_browser_hidden_quote()) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == ("timeout", True)


HELP_QUOTE = "If you can’t join this video call, ask the host for help"
CURLY_REFUSAL = "<h1>You can’t join this video call</h1>"


async def test_a_help_quote_before_the_knock_is_not_a_refusal() -> None:
    page = _refusal_before_the_knock(
        "can’t",
        labels={
            VISIBLE_BODY_SELECTOR: [f"{HELP_QUOTE}\nJoin as guest\nSign in"],
            VISIBLE_CONTROLS_SELECTOR: ["Join as guest", "Sign in"],
        },
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("failed", False)


async def test_a_help_quote_on_the_waiting_page_keeps_waiting() -> None:
    waiting = f"<div>Asking to be let in...</div><p>{HELP_QUOTE}</p>"
    page = meet_page([waiting] * 8, evaluates=[NO_CAPTCHA] * 8)

    result = await _join(page)

    assert (result.status, result.knocked) == ("timeout", True)


async def test_a_hidden_stale_join_button_does_not_cancel_a_shown_refusal() -> None:
    page = meet_page(
        [CURLY_REFUSAL + "<button hidden>Ask to join</button>"], evaluates=[NO_CAPTCHA]
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", True)


@pytest.mark.parametrize("spelling", SPELLINGS, ids=SPELLING_IDS)
async def test_a_refusal_beside_an_ended_heading_keeps_the_ascii_priority(spelling: str) -> None:
    page = meet_page(
        [f"<h1>You {spelling} join this video call</h1><h2>The meeting has ended</h2>"],
        evaluates=[NO_CAPTCHA],
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", True)


def _captcha_refusal(captchas: list[bool], name_counts: list[int], **script: Any) -> Any:
    return meet_page(
        [_heading("can’t")] * 6,
        evaluates=captchas,
        counts={NAME_SELECTOR: name_counts},
        labels={
            VISIBLE_BODY_SELECTOR: ["You can’t join this video call\nReturn to home screen"],
            VISIBLE_CONTROLS_SELECTOR: ["Return to home screen"],
        },
        **script,
    )


async def test_a_lasting_captcha_with_a_refusal_before_the_knock_ends_after_the_grace() -> None:
    page = _captcha_refusal([LIVE_CAPTCHA] * 4, [0])
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=6.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("blocked", False)
    assert result.detail.startswith("captcha challenge went unsolved")
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert page.typed == []
    assert ASK_SELECTOR not in page.clicked


async def test_a_lasting_captcha_before_the_knock_is_never_retried() -> None:
    page = _captcha_refusal([LIVE_CAPTCHA] * 80, [0])
    browser = FakePageSession(page)
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(meeting=MeetingSettings(join_max_attempts=3)),
        SessionWiring(
            open_page=browser.open, sleep=clock.sleep, monotonic=clock.monotonic, journal=journal
        ),
    )

    assert (result.phase, result.terminal_reason) == ("ended", "blocked_by_platform")
    attempts = journal.events("join_attempt_finished")
    assert [(row["attempt"], row["status"]) for row in attempts] == [(1, "blocked")]
    assert len(page.visited) == 1
    assert page.typed == []


async def test_a_captcha_that_clears_lets_the_form_continue_to_one_knock() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[LIVE_CAPTCHA],
        counts={NAME_SELECTOR: [0, 1]},
        label_script={
            VISIBLE_BODY_SELECTOR: [["You can’t join this video call"], ["Your name\nAsk to join"]],
            VISIBLE_CONTROLS_SELECTOR: [["Return to home screen"], ["Ask to join"]],
        },
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=6.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("admitted", True)
    assert page.typed == ["Twin"]
    assert page.clicked.count(ASK_SELECTOR) == 1


def _browser_class_hidden(*, shown: bool) -> str:
    hidden = "" if shown else ' class="gone"'
    waiting = "" if shown else "<div>' + ['Asking', 'to be let in...'].join(' ') + '</div>"
    return (
        "<style>.gone { display: none; }</style>"
        "<input placeholder='Your name'><button id='ask'>Ask to join</button><script>"
        "document.querySelector('#ask').onclick = () => { setTimeout(() => {"
        f"document.body.innerHTML = '<div{hidden}>You can’t join this video call</div>{waiting}';"
        "}, 300); };</script>"
    )


@pytest.mark.browser
@pytest.mark.parametrize("variant", ["class-hidden", "shown"])
async def test_a_refusal_hidden_by_a_css_class_keeps_waiting_and_its_shown_pair_blocks(
    variant: str,
) -> None:
    shown = variant == "shown"
    expected = "blocked" if shown else "timeout"
    async with _browser_meeting(_browser_class_hidden(shown=shown)) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == (expected, True)


@pytest.mark.browser
async def test_a_real_help_quote_before_the_knock_is_not_a_refusal() -> None:
    body = (
        "<input placeholder='Your name' style='display:none'>"
        f"<p>{HELP_QUOTE}</p><button>Join as guest</button><button>Sign in</button>"
    )
    async with _browser_meeting(body) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == ("failed", False)


async def test_a_prepare_deadline_during_a_captcha_refusal_is_terminal_before_the_knock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meet, "PREPARE_TIMEOUT_SECONDS", 0.5)
    page = _captcha_refusal([LIVE_CAPTCHA] * 200, [0])

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=30.0, poll_interval_seconds=0.05),
        polling=JoinPolling(),
    )

    assert (result.status, result.knocked) == ("blocked", False)
    assert result.detail.startswith("captcha challenge went unsolved")
    assert page.typed == []


INLINE_REFUSAL = "<h1>You can’t <span>join</span> this video call</h1>"
INLINE_HELP = "<p>If <strong>you can’t join this video call</strong>, ask the host.</p>"


async def test_a_refusal_split_by_inline_markup_after_the_knock_is_blocked() -> None:
    page = meet_page([INLINE_REFUSAL + HOME], evaluates=[NO_CAPTCHA])

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", True)


async def test_an_inline_emphasised_help_sentence_after_the_knock_keeps_waiting() -> None:
    page = meet_page([INLINE_HELP] * 8, evaluates=[NO_CAPTCHA] * 8)

    result = await _join(page)

    assert (result.status, result.knocked) == ("timeout", True)


async def test_a_help_control_named_like_ask_to_join_does_not_cancel_a_refusal() -> None:
    page = meet_page(
        [_plain("can’t") + "<button>Ask to join instructions</button>"], evaluates=[NO_CAPTCHA]
    )

    result = await _join(page)

    assert (result.status, result.knocked) == ("blocked", True)


async def test_a_full_sentence_signal_recognises_the_same_refusal() -> None:
    page = meet_page([_heading("can’t")], evaluates=[NO_CAPTCHA])

    result = await _join(page, signals=MeetSignals(denied="You can't join this video call"))

    assert (result.status, result.knocked) == ("blocked", True)


@pytest.mark.parametrize("field", ["editable", "hidden"])
async def test_a_lasting_captcha_refusal_never_types_and_ends_at_the_grace(field: str) -> None:
    script: dict[str, Any] = {"wait_flaky": {NAME_SELECTOR: 1000}} if field == "hidden" else {}
    page = _captcha_refusal([LIVE_CAPTCHA] * 200, [1], **script)
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=120.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("blocked", False)
    assert result.detail.startswith("captcha challenge went unsolved")
    assert page.typed == []
    assert page.clicked == []
    assert clock.monotonic() >= 120.0


async def test_a_captcha_that_clears_beside_an_editable_field_types_once_and_knocks_once() -> None:
    page = meet_page(
        [IN_CALL],
        evaluates=[LIVE_CAPTCHA],
        counts={NAME_SELECTOR: [1]},
        label_script={
            VISIBLE_BODY_SELECTOR: [["You can’t join this video call"], ["Ask to join"]],
            VISIBLE_CONTROLS_SELECTOR: [["Return to home screen"], ["Ask to join"]],
        },
    )
    clock = FakeClock()

    result = await request_guest_join(
        page,
        _request(captcha_grace_seconds=6.0, poll_interval_seconds=2.0),
        polling=JoinPolling(clock.sleep, clock.monotonic),
    )

    assert (result.status, result.knocked) == ("admitted", True)
    assert page.typed == ["Twin"]
    assert page.clicked.count(ASK_SELECTOR) == 1


@pytest.mark.browser
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_a_real_refusal_split_by_inline_markup_is_blocked(stage: str) -> None:
    message = "You can’t <span>join</span> this video call"
    body = (
        _browser_refusal("can’t", "h1").replace("You can’t join this video call", message)
        if stage == "before"
        else _browser_after_knock("can’t").replace("You can’t join this video call", message)
    )
    async with _browser_meeting(body) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == ("blocked", stage == "after")


@pytest.mark.browser
async def test_a_real_emphasised_help_sentence_before_the_knock_is_not_a_refusal() -> None:
    body = (
        "<input placeholder='Your name' style='display:none'>"
        + INLINE_HELP
        + "<button>Join as guest</button><button>Sign in</button>"
    )
    async with _browser_meeting(body) as page:
        result = await _browser_join(page)

    assert (result.status, result.knocked) == ("failed", False)
