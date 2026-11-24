import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from functools import partial
from html.parser import HTMLParser
from typing import Literal, override
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from cloakbrowser.human.actionability import ActionabilityError
from playwright.async_api import ElementHandle, Locator, Page
from playwright.async_api import Error as PlaywrightError

from fakeyou.config.meeting_request import meeting_code
from fakeyou.config.settings import DEFAULTS
from fakeyou.foundation.invariants import POLL_TIMEOUT_MULTIPLIER

JoinStatus = Literal["admitted", "denied", "blocked", "timeout", "aborted", "failed", "ended"]
CallState = Literal["in_call", "removed", "ended", "lobby", "left", "unknown"]
LeaveStatus = Literal["left", "failed"]
Destination = Literal[
    "the requested meeting", "the Meet home page", "a different Meet page", "another origin"
]
Admission = Literal["protocol", "dom"]
ControlsOutcome = Literal["ready", "toolbar_missing", "mic_control_missing", "stopped"]

EXPLICIT_DENIAL_MARKERS = (
    "denied your request",
    "request to join was denied",
    "you were denied",
    "weren't allowed to join",
    "weren’t allowed to join",
    "not allowed to join",
    "not admitted",
    "ask to join again",
)

WAITING_ROOM_MARKERS = (
    "asking to be let in",
    "join the call when someone lets you",
    "please wait until a meeting host brings you into the call",
    "waiting for the host to let you in",
    "you're in the waiting room",
)

_LOCATION_PROBE = "(() => location.href)()"
HEADING_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Meet home", ("video calls and meetings for everyone", "premium video meetings")),
    ("pre-join", ("ready to join", "what's your name", "what\u2019s your name")),
    ("getting ready", ("getting ready",)),
    ("access refused", ("can't join this video call", "can\u2019t join this video call")),
    ("call ended", ("call has ended", "you left the meeting", "call ended")),
    ("removed", ("been removed",)),
    ("sign-in", ("sign in",)),
    ("invalid meeting code", ("check your meeting code", "invalid video call name")),
)
DENIAL_REASONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("call-ended marker", ("call has ended", "meeting has ended")),
    ("host admission required", ("unless invited or admitted", "admitted by the host")),
    ("meeting full", ("meeting is full", "participant limit")),
    ("sign-in required", ("sign in to join", "use your google account")),
    (
        "organization restricted",
        (
            "outside your organization",
            "only people in your organization",
            "only people from your organization",
        ),
    ),
)
CONTROL_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("leave call", ("leave call",)),
    (
        "microphone toggle",
        (
            "turn on microphone",
            "turn off microphone",
            "turn on your microphone",
            "turn off your microphone",
            "mute microphone",
            "unmute microphone",
        ),
    ),
    (
        "camera toggle",
        ("turn on camera", "turn off camera", "turn on your camera", "turn off your camera"),
    ),
    ("ask to join", ("ask to join",)),
    ("join now", ("join now",)),
    ("return to home screen", ("return to home screen",)),
    ("submit feedback", ("submit feedback",)),
    ("notice dismiss", ("got it", "dismiss", "close")),
    ("sign-in or sign-up", ("sign in", "sign up", "get started", "learn more")),
    ("participant tile action", ("pin ", "unpin ", "more options for ", "remove ", "mute ")),
    (
        "call toolbar",
        (
            "chat with everyone",
            "show everyone",
            "people",
            "raise hand",
            "lower hand",
            "send a reaction",
            "present now",
            "share screen",
            "more options",
            "meeting details",
            "activities",
            "turn on captions",
            "turn off captions",
        ),
    ),
)
CONTROL_LABEL_LIMIT = 40
PREPARE_TIMEOUT_SECONDS = 120.0
CALL_STATE_TIMEOUT_SECONDS = 5.0
LOBBY_READ_FAILURE_LIMIT = 5
CAPTCHA_PROBE_TIMEOUT_SECONDS = 2.0

_CAPTCHA_PROBE = """(() => {
  try {
    const frames = document.querySelectorAll('iframe[src*="recaptcha"]');
    for (const frame of frames) {
      const rect = frame.getBoundingClientRect();
      const style = frame.ownerDocument.defaultView.getComputedStyle(frame);
      if (style.visibility === "hidden" || style.display === "none") continue;
      if (rect.width < 120 || rect.height < 40) continue;
      return true;
    }
  } catch {}
  return false;
})()"""

TYPE_DELAY_MS = 90
_CLICK_ATTEMPTS = 3
NAME_FIELD_ATTEMPTS = 25
_CLICK_TIMEOUT_MS = 5000
_CLICK_WAIT_MS = 2000
_CLICK_RETRY_DELAY_SECONDS = 1.0
CALL_CONTROLS_TIMEOUT_SECONDS = 20.0
PROBE_TIMEOUT_SECONDS = 2.0
MIC_ENABLED_SELECTOR = (
    'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
)
MIC_DISABLED_SELECTOR = (
    'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
)
MIC_CONTROL_SELECTOR = f"{MIC_ENABLED_SELECTOR},{MIC_DISABLED_SELECTOR}"


@dataclass(frozen=True, slots=True)
class MeetSignals:
    name_field_labels: tuple[str, ...] = ("Your name", "Enter your name")
    ask_to_join: str = "Ask to join"
    join_now: str = "Join now"
    leave_call: str = "Leave call"
    denied: str = "can't join this"
    removed: str = "been removed"
    ended: str = "ended"
    left: str = "left the meeting"
    continue_without_media: str = "Continue without microphone"
    guest_mode_labels: tuple[str, ...] = ("Join as guest", "Continue as guest")
    submit_variants: tuple[str, ...] = ("Ask to join without camera",)


DEFAULT_MEET_SIGNALS = MeetSignals()
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
ACCESSIBLE_NAME_PROBE = """node => {
  const referenced = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
    .map((id) => id && document.getElementById(id)).filter(Boolean)
    .map((element) => element.textContent).join(' ').trim();
  if (referenced) return referenced;
  const label = (node.getAttribute('aria-label') || '').trim();
  if (label) return label;
  const shown = (element) => element.getAttribute('aria-hidden') !== 'true'
    && getComputedStyle(element).display !== 'none';
  const walk = (element) => [...element.childNodes].map((child) => {
    if (child.nodeType === Node.TEXT_NODE) return child.textContent;
    if (child.nodeType === Node.ELEMENT_NODE && shown(child)) return walk(child);
    return '';
  }).join(' ');
  return walk(node);
}"""
VISIBLE_BODY_SELECTOR = "body"
VISIBLE_CONTROLS_SELECTOR = "button, [role='button']"
_VISIBLE_TEXT_PROBE = "nodes => nodes.flatMap((node) => (node.innerText || '').split('\\n'))"
_VISIBLE_CONTROLS_PROBE = """nodes => nodes
  .filter((node) => node.checkVisibility({ checkVisibilityCSS: true }))
  .map((node) => {
    const referenced = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
      .map((id) => id && document.getElementById(id)).filter(Boolean)
      .map((element) => element.textContent).join(' ').trim();
    return referenced || (node.getAttribute('aria-label') || '').trim()
      || (node.innerText || '').trim();
  })"""
_HEADING_TAGS = frozenset({"h1", "h2", "h3"})
_NON_TEXT_TAGS = frozenset({"script", "style", "template", "head", "title"})
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


@dataclass(frozen=True, slots=True)
class GuestJoinRequest:
    link: str
    twin_name: str
    lobby_timeout_seconds: float = DEFAULTS.meeting.lobby_timeout_seconds
    poll_interval_seconds: float = DEFAULTS.meeting.join_poll_seconds
    button_timeout_seconds: float = 10.0
    captcha_grace_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class JoinResult:
    status: JoinStatus
    detail: str
    knocked: bool = False
    admission: Admission | None = None


@dataclass(frozen=True, slots=True)
class CallObservation:
    state: CallState
    detail: str
    headings: str = ""


@dataclass(frozen=True, slots=True)
class LeaveResult:
    status: LeaveStatus
    detail: str


def with_pinned_meet_locale(link: str, locale: str = "en-US") -> str:
    language = (locale.split("-", maxsplit=1)[0] or locale).strip()
    if not language:
        return link
    try:
        parts = urlsplit(link)
        query = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return link
    if any(key == "hl" for key, _ in query):
        return link
    query.append(("hl", language))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _is_explicit_denial(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in EXPLICIT_DENIAL_MARKERS)


def _is_waiting(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in WAITING_ROOM_MARKERS)


def _admitted_marker(signals: MeetSignals) -> str:
    return f'aria-label="{signals.leave_call}"'


def _is_in_call(content: str, signals: MeetSignals) -> bool:
    return _admitted_marker(signals) in content and not _is_waiting(content)


async def _page_location(page: Page, *, poll_interval_seconds: float) -> str | None:
    try:
        async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * poll_interval_seconds):
            current = await page.evaluate(_LOCATION_PROBE)
    except (PlaywrightError, TimeoutError):
        return None
    return str(current)


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parts = urlparse(url)
    try:
        port = parts.port
    except ValueError:
        port = -1
    return parts.scheme.lower(), parts.hostname, port


def landed_destination(landed: str, link: str) -> Destination:
    actual = urlparse(landed)
    if _origin(landed) != _origin(link):
        return "another origin"
    if actual.path in ("", "/"):
        return "the Meet home page"
    code = meeting_code(landed)
    if code is not None and code == meeting_code(link):
        return "the requested meeting"
    return "a different Meet page"


def _landed_failure(landed: str, link: str) -> str | None:
    destination = landed_destination(landed, link)
    if destination == "the requested meeting":
        return None
    if destination == "another origin":
        return f"navigation landed on an unexpected host: {urlparse(landed).hostname or 'unknown'}"
    return f"navigation landed on {destination} instead of the requested meeting"


def heading_category(heading: str) -> str:
    lowered = heading.casefold()
    for category, markers in HEADING_CATEGORIES:
        if any(marker in lowered for marker in markers):
            return category
    return "unrecognised"


async def _page_heading(page: Page) -> str:
    try:
        headings = await page.locator("h1, h2").evaluate_all(
            "nodes => nodes.map(node => node.textContent.trim()).filter(Boolean)"
        )
    except PlaywrightError:
        return "unknown"
    categories = dict.fromkeys(heading_category(str(heading)) for heading in headings)
    return ", ".join(categories) or "none"


async def _destination_failure(page: Page, link: str) -> str | None:
    navigation = _landed_failure(page.url, link)
    if navigation is None:
        return None
    return f"{navigation}; heading: {await _page_heading(page)}"


async def _has_live_captcha(page: Page) -> bool:
    try:
        async with asyncio.timeout(CAPTCHA_PROBE_TIMEOUT_SECONDS):
            return await page.evaluate(_CAPTCHA_PROBE) is True
    except (PlaywrightError, TimeoutError):
        return False


async def _open_meeting_page(
    page: Page, link: str, init_scripts: Sequence[str] = (), *, poll_interval_seconds: float
) -> str | None:
    target = with_pinned_meet_locale(link)
    for script in init_scripts:
        await page.add_init_script(script)
    try:
        response = await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    except PlaywrightError:
        response = None
        failed = True
    else:
        failed = response is not None and not response.ok
    if not failed:
        return await _destination_failure(page, link)
    current = await _page_location(page, poll_interval_seconds=poll_interval_seconds)
    if current is not None and _landed_failure(current, link) is None:
        return None
    destination = landed_destination(current, link) if current is not None else "unknown"
    return f"navigation to the meeting link failed; current destination: {destination}"


async def _first_match(locator: Locator) -> Locator:
    return locator.first


async def _usable_match(locator: Locator) -> Locator | None:
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible() and await candidate.is_editable():
            return candidate
    return None


async def _first_usable_match(locator: Locator) -> Locator:
    return await _usable_match(locator) or locator.first


async def _find_name_field(page: Page, signals: MeetSignals) -> Locator | None:
    for name in signals.name_field_labels:
        usable = await _usable_match(page.get_by_placeholder(name))
        if usable is not None:
            return usable
    try:
        frames = page.frames[1:]
    except (PlaywrightError, AttributeError):
        frames = []
    for frame in frames:
        for name in signals.name_field_labels:
            try:
                usable = await _usable_match(frame.get_by_placeholder(name))
            except PlaywrightError:
                continue
            if usable is not None:
                return usable
    return None


async def _first_visible_match(locator: Locator) -> Locator:
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        if await candidate.is_visible():
            return candidate
    return locator.first


async def click_when_ready(
    locate: Callable[[], Locator],
    label: str,
    sleep: Callable[[float], Awaitable[None]],
    attempts: int = _CLICK_ATTEMPTS,
    choose: Callable[[Locator], Awaitable[Locator]] = _first_match,
) -> str | None:
    last_error = "locator never became visible"
    for _ in range(attempts):
        try:
            target = await choose(locate())
            await target.wait_for(state="visible", timeout=_CLICK_WAIT_MS)
        except (PlaywrightError, ActionabilityError):
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        try:
            await target.click(timeout=_CLICK_TIMEOUT_MS)
        except (PlaywrightError, ActionabilityError) as error:
            last_error = type(error).__name__
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        return None
    return f"clicking {label!r} failed after {attempts} attempts: {last_error}"


async def _dismiss_media_dialog(
    page: Page,
    signals: MeetSignals,
    sleep: Callable[[float], Awaitable[None]],
) -> str | None:
    locator = page.locator(f"text={signals.continue_without_media}")
    try:
        if await locator.count() == 0:
            return None
    except PlaywrightError as error:
        return f"finding {signals.continue_without_media!r} failed: ({type(error).__name__})"
    return await click_when_ready(
        partial(page.locator, f"text={signals.continue_without_media}"),
        signals.continue_without_media,
        sleep,
    )


async def _name_field_failure(page: Page, link: str, failure: str) -> str:
    navigation = await _destination_failure(page, link)
    if navigation is not None:
        return navigation
    return (
        f"{failure}; destination: the requested meeting; heading: {await _page_heading(page)}; "
        f"visible controls: {await visible_control_categories(page)}"
    )


_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})
ACCESSIBLE_NAME_PROBE = """node => {
  const referenced = (node.getAttribute('aria-labelledby') || '').split(/\\s+/)
    .map((id) => id && document.getElementById(id)).filter(Boolean)
    .map((element) => element.textContent).join(' ').trim();
  if (referenced) return referenced;
  const label = (node.getAttribute('aria-label') || '').trim();
  if (label) return label;
  const shown = (element) => element.getAttribute('aria-hidden') !== 'true'
    && getComputedStyle(element).display !== 'none';
  const walk = (element) => [...element.childNodes].map((child) => {
    if (child.nodeType === Node.TEXT_NODE) return child.textContent;
    if (child.nodeType === Node.ELEMENT_NODE && shown(child)) return walk(child);
    return '';
  }).join(' ');
  return walk(node);
}"""


class _GuestRefusedError(Exception):
    def __init__(self, result: JoinResult) -> None:
        super().__init__(result.status)
        self.result = result


def plain_text(text: str) -> str:
    return " ".join(text.translate(_APOSTROPHES).casefold().split())


def _xpath_literal(word: str) -> str:
    if "'" not in word:
        return f"'{word}'"
    if '"' not in word:
        return f'"{word}"'
    return "concat('" + "', \"'\", '".join(word.split("'")) + "')"


def control_selector(name: str) -> str:
    words = plain_text(name).split() or [""]
    lowered = [
        f"translate(normalize-space({source}), '{_UPPER}', '{_LOWER}')"
        for source in (
            *(f"id(@aria-labelledby)[{position}]" for position in range(1, 5)),
            "@aria-label",
            ".",
        )
    ]
    matches = " and ".join(
        "(" + " or ".join(f"contains({source}, {_xpath_literal(word)})" for source in lowered) + ")"
        for word in words
    )
    return f"xpath=//*[self::button or @role='button'][{matches}]"


async def _official_handle(page: Page, name: str) -> ElementHandle | None:
    locator = page.locator(control_selector(name))
    for index in range(await locator.count()):
        try:
            handle = await locator.nth(index).element_handle()
        except PlaywrightError:
            continue
        if handle is None:
            continue
        if await _handle_is_official(handle, name):
            return handle
    return None


async def _handle_is_official(handle: ElementHandle, name: str) -> bool:
    try:
        if not (await handle.is_visible() and await handle.is_enabled()):
            return False
        return plain_text(str(await handle.evaluate(ACCESSIBLE_NAME_PROBE))) == plain_text(name)
    except PlaywrightError:
        return False


async def click_official_when_ready(
    page: Page,
    name: str,
    sleep: Callable[[float], Awaitable[None]],
    attempts: int = _CLICK_ATTEMPTS,
    guard: Callable[[], Awaitable[JoinResult | None]] | None = None,
) -> str | JoinResult | None:
    last_error = "locator never became visible"
    for _ in range(attempts):
        if guard is not None:
            held = await guard()
            if held is not None:
                return held
        try:
            handle = await _official_handle(page, name)
        except PlaywrightError as error:
            last_error = type(error).__name__
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        if handle is None or not await _handle_is_official(handle, name):
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        try:
            await handle.hover(timeout=_CLICK_WAIT_MS, force=True)
            if guard is not None:
                held = await guard()
                if held is not None:
                    return held
            if not await _handle_is_official(handle, name):
                await sleep(_CLICK_RETRY_DELAY_SECONDS)
                continue
            await handle.click(timeout=_CLICK_TIMEOUT_MS)
        except (PlaywrightError, ActionabilityError) as error:
            last_error = type(error).__name__
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        return None
    return f"clicking {name!r} failed after {attempts} attempts: {last_error}"


async def _choose_guest_mode(page: Page, signals: MeetSignals) -> str | None:
    for name in signals.guest_mode_labels:
        try:
            handle = await _official_handle(page, name)
        except PlaywrightError:
            continue
        if handle is None or not await _handle_is_official(handle, name):
            continue
        try:
            await handle.hover(timeout=_CLICK_WAIT_MS, force=True)
            if not await _handle_is_official(handle, name):
                continue
            await handle.click(timeout=_CLICK_TIMEOUT_MS)
        except (PlaywrightError, ActionabilityError) as error:
            return f"click failed ({type(error).__name__})"
        return "chosen"
    return None


def _lines(chunks: Iterable[object]) -> list[str]:
    return [
        plain_text(line) for chunk in chunks for line in str(chunk).splitlines() if line.strip()
    ]


def refusal_in(chunks: Iterable[object], controls: Iterable[object], signals: MeetSignals) -> bool:
    lines = _lines(chunks)
    if any(_is_waiting(line) for line in lines):
        return False
    phrase = plain_text(signals.denied).removeprefix("you ")
    if not any(line.removeprefix("you ").startswith(phrase) for line in lines):
        return False
    official = {plain_text(label) for label in (signals.leave_call, *submit_names(signals))}
    return not official.intersection(_lines(controls))


async def _page_refusal(page: Page, signals: MeetSignals) -> bool:
    try:
        async with asyncio.timeout(CALL_STATE_TIMEOUT_SECONDS):
            chunks = await page.locator(VISIBLE_BODY_SELECTOR).evaluate_all(_VISIBLE_TEXT_PROBE)
            controls = await page.locator(VISIBLE_CONTROLS_SELECTOR).evaluate_all(
                _VISIBLE_CONTROLS_PROBE
            )
    except (PlaywrightError, TimeoutError):
        return False
    return refusal_in(chunks, controls, signals)


async def _page_says(page: Page) -> str:
    try:
        async with asyncio.timeout(CALL_STATE_TIMEOUT_SECONDS):
            return denial_summary(await page.content())
    except (PlaywrightError, TimeoutError):
        return "unknown"


@dataclass(frozen=True, slots=True)
class JoinPolling:
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    abort: Callable[[], bool] | None = None
    admitted: Callable[[], bool] | None = None


DEFAULT_JOIN_POLLING = JoinPolling()


@dataclass(slots=True)
class _BeforeKnock:
    page: Page
    request: GuestJoinRequest
    signals: MeetSignals
    polling: JoinPolling
    captcha_since: float | None = None

    async def verdict(self) -> JoinResult | None:
        refused = _landed_failure(self.page.url, self.request.link) is None and (
            await _page_refusal(self.page, self.signals)
        )
        if not await _has_live_captcha(self.page):
            self.captcha_since = None
            if not refused:
                return None
            return JoinResult(
                status="blocked",
                detail="platform refused the guest before the knock; "
                f"page says: {await _page_says(self.page)}",
            )
        now = self.polling.monotonic()
        if self.captcha_since is None:
            self.captcha_since = now
        if now - self.captcha_since >= self.request.captcha_grace_seconds:
            return await self.unsolved_captcha()
        return None

    async def settled(self) -> JoinResult | None:
        while True:
            refusal = await self.verdict()
            if refusal is not None or self.captcha_since is None:
                return refusal
            await self.polling.sleep(self.request.poll_interval_seconds)

    async def unsolved_captcha(self) -> JoinResult:
        return JoinResult(
            status="blocked",
            detail="captcha challenge went unsolved; platform blocked the guest before the knock; "
            f"page says: {await _page_says(self.page)}",
        )


async def _await_name_field(
    page: Page, request: GuestJoinRequest, signals: MeetSignals, watch: _BeforeKnock
) -> str | JoinResult | None:
    while True:
        refusal = await watch.verdict()
        if refusal is not None:
            return refusal
        if watch.captcha_since is not None:
            await watch.polling.sleep(request.poll_interval_seconds)
            continue
        try:
            if await _find_name_field(page, signals) is not None:
                return None
        except PlaywrightError as error:
            return f"finding the name field failed: ({type(error).__name__})"
        return await _name_field_failure(
            page,
            request.link,
            f"name field {signals.name_field_labels!r} not found on the lobby page",
        )


async def _enter_twin_name(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals,
    watch: _BeforeKnock,
) -> str | JoinResult | None:
    guest_mode: str | None = None
    waited = 0
    while waited < NAME_FIELD_ATTEMPTS:
        failure = await _destination_failure(page, request.link)
        if failure is not None:
            return failure
        refusal = await watch.verdict()
        if refusal is not None:
            return refusal
        try:
            usable = await _find_name_field(page, signals)
            if usable is None and guest_mode is None and watch.captcha_since is None:
                guest_mode = await _choose_guest_mode(page, signals)
        except PlaywrightError as error:
            return f"finding the name field failed: ({type(error).__name__})"
        if usable is not None and watch.captcha_since is None:
            return await _type_twin_name(
                page, request, signals, watch, NAME_FIELD_ATTEMPTS - waited
            )
        if watch.captcha_since is None:
            waited += 1
            await watch.polling.sleep(_CLICK_RETRY_DELAY_SECONDS)
        else:
            await watch.polling.sleep(request.poll_interval_seconds)
    return await _name_field_failure(
        page,
        request.link,
        f"name field {signals.name_field_labels!r} not usable on the lobby page; "
        f"guest mode: {guest_mode or 'not offered'}",
    )


async def _type_twin_name(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals,
    watch: _BeforeKnock,
    attempts: int,
) -> str | JoinResult | None:
    async def usable_unless_refused(locator: Locator) -> Locator:
        refusal = await watch.settled()
        if refusal is not None:
            raise _GuestRefusedError(refusal)
        found = await _find_name_field(page, signals)
        if found is not None:
            return found
        return await _first_usable_match(locator)

    try:
        failure = await click_when_ready(
            partial(page.get_by_placeholder, signals.name_field_labels[0]),
            "name field",
            watch.polling.sleep,
            attempts,
            choose=usable_unless_refused,
        )
    except _GuestRefusedError as refused:
        return refused.result
    if failure is not None:
        if watch.captcha_since is not None:
            return await watch.unsolved_captcha()
        return await _name_field_failure(page, request.link, failure)
    refusal = await watch.settled()
    if refusal is not None:
        return refusal
    failure = await _destination_failure(page, request.link)
    if failure is not None:
        return failure
    try:
        await page.keyboard.type(request.twin_name, delay=TYPE_DELAY_MS)
    except PlaywrightError as error:
        return f"typing the twin name failed: ({type(error).__name__})"
    return None


async def _knock_landed(page: Page) -> bool:
    try:
        async with asyncio.timeout(CALL_STATE_TIMEOUT_SECONDS):
            content = await page.content()
    except (PlaywrightError, TimeoutError):
        return False
    return _is_waiting(content)


def submit_names(signals: MeetSignals) -> tuple[str, ...]:
    return (signals.ask_to_join, *signals.submit_variants, signals.join_now)


async def _poll_official_submit(
    page: Page,
    signals: MeetSignals,
    watch: _BeforeKnock,
    hold_or_refuse: Callable[[], Awaitable[JoinResult | None]],
) -> tuple[bool, str | JoinResult | None]:
    for name in submit_names(signals):
        try:
            offered = await _official_handle(page, name)
        except PlaywrightError:
            continue
        if offered is None:
            continue
        failure = await click_official_when_ready(
            page, name, watch.polling.sleep, guard=hold_or_refuse
        )
        if isinstance(failure, JoinResult):
            return True, failure
        if failure is not None and not await _knock_landed(page):
            return True, failure
        return True, None
    return False, None


async def _click_join(
    page: Page,
    signals: MeetSignals,
    timeout_seconds: float,
    watch: _BeforeKnock,
) -> str | JoinResult | None:
    polling = watch.polling
    deadline = polling.monotonic() + timeout_seconds

    async def hold_or_refuse() -> JoinResult | None:
        refusal = await watch.verdict()
        if refusal is not None:
            return refusal
        if watch.captcha_since is not None:
            return await watch.settled()
        return None

    while True:
        refusal = await watch.verdict()
        if refusal is not None:
            return refusal
        if watch.captcha_since is not None:
            await polling.sleep(watch.request.poll_interval_seconds)
            continue
        handled, outcome = await _poll_official_submit(page, signals, watch, hold_or_refuse)
        if handled:
            return outcome
        if polling.monotonic() >= deadline:
            return "join button did not become available on the lobby page"
        await polling.sleep(1.0)


async def _prepare_lobby(
    page: Page,
    signals: MeetSignals,
    watch: _BeforeKnock,
    init_scripts: Sequence[str],
) -> str | JoinResult | None:
    request, polling = watch.request, watch.polling
    failure = await _open_meeting_page(
        page, request.link, init_scripts, poll_interval_seconds=request.poll_interval_seconds
    )
    if failure is not None:
        return failure
    failure = await _dismiss_media_dialog(page, signals, polling.sleep)
    if failure is not None:
        return failure
    failure = await _destination_failure(page, request.link)
    if failure is not None:
        return failure
    entered = await _enter_twin_name(page, request, signals, watch)
    if entered is not None:
        return entered
    failure = await _destination_failure(page, request.link)
    if failure is not None:
        return failure
    refusal = await watch.settled()
    if refusal is not None:
        return refusal
    return await _click_join(page, signals, request.button_timeout_seconds, watch)


async def _poll_content(
    page: Page, abort: Callable[[], bool] | None, *, poll_interval_seconds: float
) -> str | JoinResult:
    if abort is not None and abort():
        return JoinResult(status="aborted", detail="stop requested while waiting for admission")
    try:
        async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * poll_interval_seconds):
            return await page.content()
    except TimeoutError:
        return JoinResult(
            status="failed",
            detail="reading lobby page content timed out",
        )
    except PlaywrightError as error:
        return JoinResult(
            status="failed",
            detail=f"reading lobby page content failed: ({type(error).__name__})",
        )


def denial_summary(content: str) -> str:
    parsed = _parsed_markers(content)
    headings = " ".join(parsed.headings)
    message = " ".join(parsed.text).casefold()
    reasons = [
        reason
        for reason, markers in DENIAL_REASONS
        if any(marker in message for marker in markers)
        or (reason == "call-ended marker" and parsed.call_ended)
    ]
    heading = heading_category(headings) if headings.strip() else "none"
    return f"{heading} ({', '.join(reasons) or 'unrecognised'})"


def _classify_denial(content: str, signals: MeetSignals) -> JoinResult | None:
    if _is_explicit_denial(content):
        return JoinResult(status="denied", detail="guest request to join was denied")
    headings, _buttons = call_markers(content)
    if plain_text(signals.denied) in plain_text(headings):
        return None
    if signals.ended in headings:
        return JoinResult(
            status="ended", detail="the meeting ended while the twin was waiting in the lobby"
        )
    return None


async def _poll_captcha(
    page: Page,
    polling: JoinPolling,
    captcha_since: float | None,
    request: GuestJoinRequest,
) -> tuple[JoinResult | None, float | None]:
    if not await _has_live_captcha(page):
        return JoinResult(status="blocked", detail="platform blocked the guest"), captcha_since
    now = polling.monotonic()
    if captcha_since is None:
        captcha_since = now
    if now - captcha_since < request.captcha_grace_seconds:
        await polling.sleep(request.poll_interval_seconds)
        return None, captcha_since
    return (
        JoinResult(
            status="blocked",
            detail="captcha challenge went unsolved; platform blocked the guest",
        ),
        captcha_since,
    )


async def _timeout_outcome(
    page: Page, content: str, signals: MeetSignals, request: GuestJoinRequest
) -> JoinResult:
    if _is_waiting(content):
        detail = f"still waiting for admission after {request.lobby_timeout_seconds} seconds"
    else:
        detail = f"no admission within {request.lobby_timeout_seconds} seconds"
    try:
        async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * request.poll_interval_seconds):
            rechecked = await page.content()
    except (PlaywrightError, TimeoutError):
        return JoinResult(status="timeout", detail=detail)
    if _is_in_call(rechecked, signals):
        return JoinResult(status="admitted", detail="guest admitted to the call", admission="dom")
    return JoinResult(status="timeout", detail=detail)


async def request_guest_join(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
    polling: JoinPolling = DEFAULT_JOIN_POLLING,
    init_scripts: Sequence[str] = (),
) -> JoinResult:
    attempt_timeout = (
        PREPARE_TIMEOUT_SECONDS
        + request.lobby_timeout_seconds
        + 4 * POLL_TIMEOUT_MULTIPLIER * request.poll_interval_seconds
    )
    try:
        async with asyncio.timeout(attempt_timeout):
            watch = _BeforeKnock(page, request, signals, polling)
            try:
                async with asyncio.timeout(PREPARE_TIMEOUT_SECONDS):
                    failure = await _prepare_lobby(page, signals, watch, init_scripts)
            except TimeoutError:
                if watch.captcha_since is not None:
                    return await watch.unsolved_captcha()
                return JoinResult(
                    status="failed",
                    detail=(
                        f"lobby preparation did not finish within {PREPARE_TIMEOUT_SECONDS} seconds"
                    ),
                )
            if isinstance(failure, JoinResult):
                return failure
            if failure is not None:
                return JoinResult(status="failed", detail=failure)
            return replace(await _await_admission(page, request, signals, polling), knocked=True)
    except TimeoutError:
        return JoinResult(
            status="timeout",
            detail=f"no admission within {request.lobby_timeout_seconds} seconds",
            knocked=True,
        )


def _unreadable_outcome(
    content: JoinResult, unreadable: int, request: GuestJoinRequest, *, expired: bool
) -> JoinResult | None:
    if content.status == "aborted" or unreadable >= LOBBY_READ_FAILURE_LIMIT:
        return content
    if expired:
        return JoinResult(
            status="timeout", detail=f"no admission within {request.lobby_timeout_seconds} seconds"
        )
    return None


async def _await_admission(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals,
    polling: JoinPolling,
) -> JoinResult:
    deadline = polling.monotonic() + request.lobby_timeout_seconds
    captcha_since: float | None = None
    unreadable = 0
    while True:
        if polling.admitted is not None and polling.admitted():
            return JoinResult(
                status="admitted",
                detail="own device was admitted to the meeting",
                admission="protocol",
            )
        content = await _poll_content(
            page, polling.abort, poll_interval_seconds=request.poll_interval_seconds
        )
        if isinstance(content, JoinResult):
            unreadable += 1
            ended = _unreadable_outcome(
                content, unreadable, request, expired=polling.monotonic() >= deadline
            )
            if ended is not None:
                return ended
            await polling.sleep(request.poll_interval_seconds)
            continue
        unreadable = 0
        denial = _classify_denial(content, signals)
        if denial is not None:
            return denial
        if await _page_refusal(page, signals):
            outcome, captcha_since = await _poll_captcha(page, polling, captcha_since, request)
            if outcome is None:
                continue
            return JoinResult(
                status=outcome.status,
                detail=f"{outcome.detail}; page says: {denial_summary(content)}; "
                f"visible controls: {await visible_control_categories(page)}",
            )
        if _is_in_call(content, signals):
            return JoinResult(
                status="admitted", detail="guest admitted to the call", admission="dom"
            )
        if polling.monotonic() >= deadline:
            return await _timeout_outcome(page, content, signals, request)
        await polling.sleep(request.poll_interval_seconds)


def _concealed(values: dict[str, str]) -> bool:
    style = "".join(values.get("style", "").casefold().split())
    return (
        "hidden" in values
        or values.get("aria-hidden", "").casefold() == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


class _CallMarkers(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headings: list[str] = []
        self.buttons: set[str] = set()
        self.text: list[str] = []
        self.call_ended = False
        self._open: list[tuple[str, str | None, list[str], bool]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if values.get("data-call-ended") == "true":
            self.call_ended = True
        role = values.get("role", "")
        marker: str | None = None
        if tag in _HEADING_TAGS or role == "heading":
            marker = "heading"
        elif tag == "button" or role == "button":
            marker = "button"
            if values.get("aria-label"):
                self.buttons.add(values["aria-label"].strip().lower())
        if tag not in _VOID_TAGS:
            concealed = tag in _NON_TEXT_TAGS or _concealed(values)
            self._open.append((tag, marker, [], concealed))

    @override
    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                for _tag, marker, texts, _concealed in reversed(self._open[index:]):
                    self._settle(marker, texts)
                del self._open[index:]
                return

    @override
    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if not any(concealed for _tag, _marker, _texts, concealed in self._open):
            self.text.append(text)
        for _tag, marker, texts, _concealed in reversed(self._open):
            if marker is not None:
                texts.append(text)
                return

    @override
    def close(self) -> None:
        super().close()
        for _tag, marker, texts, _concealed in reversed(self._open):
            self._settle(marker, texts)
        self._open.clear()

    def _settle(self, marker: str | None, texts: list[str]) -> None:
        text = " ".join(texts).lower()
        if not text:
            return
        if marker == "heading":
            self.headings.append(text)
        elif marker == "button":
            self.buttons.add(text)


def _parsed_markers(content: str) -> _CallMarkers:
    parser = _CallMarkers()
    parser.feed(content)
    parser.close()
    return parser


def call_markers(content: str) -> tuple[str, frozenset[str]]:
    parser = _parsed_markers(content)
    return " ".join(parser.headings), frozenset(parser.buttons)


async def observe_call_state(
    page: Page,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
) -> CallObservation:
    try:
        async with asyncio.timeout(CALL_STATE_TIMEOUT_SECONDS):
            content = await page.content()
    except TimeoutError:
        return CallObservation(state="unknown", detail="call state could not be read in time")
    headings, buttons = call_markers(content)
    if signals.removed in headings:
        state: CallState = "removed"
        detail = "guest was removed from the call"
    elif signals.ended in headings:
        state, detail = "ended", "the call has ended"
    elif signals.left in headings:
        state, detail = "left", "the guest already left the call"
    elif signals.leave_call.lower() in buttons:
        state, detail = "in_call", "call controls are visible"
    elif signals.ask_to_join.lower() in buttons or signals.join_now.lower() in buttons:
        state, detail = "lobby", "lobby controls are visible"
    else:
        state, detail = "unknown", "no recognized call marker on the page"
    return CallObservation(state=state, detail=detail, headings=headings)


async def enable_meeting_microphone(
    page: Page, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> bool:
    return await toggle_control(
        page,
        ToggleSelectors(MIC_ENABLED_SELECTOR, MIC_DISABLED_SELECTOR),
        "microphone",
        "enabled",
        sleep,
    )


@dataclass(frozen=True, slots=True)
class ControlsClock:
    sleep: Callable[[float], Awaitable[None]]
    monotonic: Callable[[], float]
    stopped: Callable[[], bool]
    poll_seconds: float
    timeout_seconds: float = CALL_CONTROLS_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ControlsWait:
    outcome: ControlsOutcome
    waited_seconds: float
    heading: str = ""


async def _visible_before(page: Page, selector: str, deadline: float, now: float) -> bool:
    remaining = deadline - now
    if remaining <= 0:
        return False
    try:
        async with asyncio.timeout(min(PROBE_TIMEOUT_SECONDS, remaining)):
            return await page.locator(selector).filter(visible=True).first.is_visible()
    except (PlaywrightError, TimeoutError):
        return False


async def _bounded_heading(page: Page) -> str:
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            return await _page_heading(page)
    except TimeoutError:
        return "unknown"


async def _appears_within(page: Page, selector: str, window: float) -> bool:
    try:
        async with asyncio.timeout(window):
            await (
                page.locator(selector)
                .filter(visible=True)
                .first.wait_for(state="visible", timeout=window * 1000)
            )
    except (PlaywrightError, TimeoutError):
        return False
    return True


async def wait_for_mic_control(
    page: Page, clock: ControlsClock, signals: MeetSignals = DEFAULT_MEET_SIGNALS
) -> ControlsWait:
    started = clock.monotonic()
    deadline = started + clock.timeout_seconds
    leave = f'button[aria-label="{signals.leave_call}"]'
    toolbar_seen = False
    while not clock.stopped():
        window_start = clock.monotonic()
        remaining = deadline - window_start
        if remaining <= 0:
            outcome: ControlsOutcome = "mic_control_missing" if toolbar_seen else "toolbar_missing"
            return ControlsWait(outcome, window_start - started, await _bounded_heading(page))
        window = min(PROBE_TIMEOUT_SECONDS, remaining)
        if await _appears_within(page, MIC_CONTROL_SELECTOR, window):
            if clock.stopped():
                break
            return ControlsWait("ready", clock.monotonic() - started)
        toolbar_seen = toolbar_seen or await _visible_before(
            page, leave, deadline, clock.monotonic()
        )
        pause = window_start + min(clock.poll_seconds, remaining) - clock.monotonic()
        if pause > 0:
            await clock.sleep(pause)
    return ControlsWait("stopped", clock.monotonic() - started)


CAMERA_DEVICE_PROBE = (
    "() => navigator.mediaDevices.enumerateDevices().then(devices => devices.map(d => d.kind))"
)


async def camera_device_present(page: Page) -> bool:
    try:
        kinds = await page.evaluate(CAMERA_DEVICE_PROBE)
    except PlaywrightError:
        return True
    if not isinstance(kinds, list) or not kinds:
        return True
    return "videoinput" in kinds


async def disable_meeting_camera(
    page: Page, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> bool:
    on = 'button[aria-label^="Turn off camera" i]'
    off = 'button[aria-label^="Turn on camera" i]'
    try:
        sending = await page.locator(on).first.is_visible()
    except PlaywrightError as error:
        raise RuntimeError(f"checking camera state failed: {error}") from error
    if not sending:
        return True
    if not await camera_device_present(page):
        return True
    return await toggle_control(page, ToggleSelectors(off, on), "camera", "disabled", sleep)


def control_category(label: str) -> str:
    lowered = label.casefold().strip()
    for category, markers in CONTROL_CATEGORIES:
        if lowered.startswith(markers):
            return category
    return "unrecognised"


async def visible_control_categories(page: Page) -> str:
    try:
        labels = await page.locator("button[aria-label]").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('aria-label')).filter(Boolean)"
        )
    except PlaywrightError:
        return "unknown"
    found = [control_category(str(label)) for label in labels[:CONTROL_LABEL_LIMIT]]
    known = [category for category, _markers in CONTROL_CATEGORIES if category in found]
    unknown = found.count("unrecognised")
    if unknown:
        known.append(f"unrecognised ({unknown})")
    return ", ".join(known) or "none"


@dataclass(frozen=True, slots=True)
class ToggleSelectors:
    desired: str
    toggle: str


async def toggle_control(
    page: Page,
    selectors: ToggleSelectors,
    control: str,
    state: str,
    sleep: Callable[[float], Awaitable[None]],
) -> bool:
    try:
        if await page.locator(selectors.desired).filter(visible=True).first.is_visible():
            return True
    except PlaywrightError as error:
        raise RuntimeError(f"checking {control} state failed: ({type(error).__name__})") from error
    failure = await click_when_ready(
        partial(page.locator, selectors.toggle),
        f"{control} button",
        sleep,
        choose=_first_visible_match,
    )
    if failure is not None:
        raise RuntimeError(f"{failure}; visible controls: {await visible_control_categories(page)}")
    try:
        await (
            page.locator(selectors.desired)
            .filter(visible=True)
            .first.wait_for(state="visible", timeout=10000)
        )
    except PlaywrightError as error:
        raise RuntimeError(
            f"waiting for {state} {control} failed: ({type(error).__name__})"
        ) from error
    return True


async def _already_out(page: Page, signals: MeetSignals) -> LeaveResult | None:
    observation = await observe_call_state(page, signals)
    if observation.state in ("removed", "ended", "lobby", "left"):
        return LeaveResult(status="left", detail=f"already out of the call: {observation.state}")
    return None


async def leave_meeting(
    page: Page,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    timeout_seconds: float = DEFAULTS.meeting.leave_timeout_seconds,
) -> LeaveResult:
    locator = page.locator(f'button[aria-label="{signals.leave_call}"]')
    try:
        found = await locator.count()
    except PlaywrightError as error:
        return LeaveResult(
            status="failed", detail=f"finding the leave button failed: ({type(error).__name__})"
        )
    if found == 0:
        out = await _already_out(page, signals)
        if out is not None:
            return out
        return LeaveResult(
            status="failed",
            detail=f"leave button not found on the page; visible controls: "
            f"{await visible_control_categories(page)}",
        )
    failure = await click_when_ready(
        partial(page.locator, f'button[aria-label="{signals.leave_call}"]'),
        "leave button",
        sleep,
    )
    if failure is not None:
        out = await _already_out(page, signals)
        if out is not None:
            return out
        return LeaveResult(status="failed", detail=failure)
    admitted_marker = _admitted_marker(signals)
    deadline = monotonic() + timeout_seconds
    while True:
        content = await page.content()
        if admitted_marker not in content:
            return LeaveResult(status="left", detail="guest left the call")
        if monotonic() >= deadline:
            return LeaveResult(status="failed", detail="leave button still visible after leaving")
        await sleep(1.0)
