import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from playwright.async_api import Locator, Page

from fakeyou.config.settings import DEFAULTS

JoinStatus = Literal["admitted", "denied", "blocked", "timeout", "aborted", "failed"]
CallState = Literal["in_call", "removed", "ended", "lobby", "unknown"]
LeaveStatus = Literal["left", "failed"]

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
NAME_FIELD_ATTEMPTS = 10
_CLICK_TIMEOUT_MS = 5000
_CLICK_WAIT_MS = 2000
_CLICK_RETRY_DELAY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class MeetSignals:
    name_field: str = "Your name"
    ask_to_join: str = "Ask to join"
    join_now: str = "Join now"
    leave_call: str = "Leave call"
    denied: str = "can't join this"
    removed: str = "been removed"
    ended: str = "has ended"
    continue_without_media: str = "Continue without microphone and camera"


DEFAULT_MEET_SIGNALS = MeetSignals()


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


@dataclass(frozen=True, slots=True)
class CallObservation:
    state: CallState
    detail: str


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


async def _page_host(page: Page) -> str | None:
    try:
        current = await page.evaluate(_LOCATION_PROBE)
    except Exception:
        return None
    return urlparse(str(current)).hostname


async def _has_live_captcha(page: Page) -> bool:
    try:
        return await page.evaluate(_CAPTCHA_PROBE) is True
    except Exception:
        return False


async def _open_meeting_page(page: Page, link: str, init_scripts: Sequence[str] = ()) -> str | None:
    target = with_pinned_meet_locale(link)
    for script in init_scripts:
        await page.add_init_script(script)
    try:
        response = await page.goto(target, wait_until="domcontentloaded", timeout=60000)
    except Exception:
        response = None
        failed = True
    else:
        failed = response is not None and not response.ok
    if not failed:
        return None
    current_host = await _page_host(page)
    if current_host is not None and current_host == urlparse(link).hostname:
        return None
    return (
        f"navigation to the meeting link failed: {target}; "
        f"current host: {current_host or 'unknown'}"
    )


async def click_when_ready(
    locate: Callable[[], Locator],
    label: str,
    sleep: Callable[[float], Awaitable[None]],
    attempts: int = _CLICK_ATTEMPTS,
) -> str | None:
    last_error = "locator never became visible"
    for _ in range(attempts):
        try:
            target = locate().first
            await target.wait_for(state="visible", timeout=_CLICK_WAIT_MS)
        except Exception:
            await sleep(_CLICK_RETRY_DELAY_SECONDS)
            continue
        try:
            await target.click(timeout=_CLICK_TIMEOUT_MS)
        except Exception as error:
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
    locator = page.locator(f'text="{signals.continue_without_media}"')
    try:
        if await locator.count() == 0:
            return None
    except Exception as error:
        return f"finding {signals.continue_without_media!r} failed: ({type(error).__name__})"
    return await click_when_ready(
        partial(page.locator, f'text="{signals.continue_without_media}"'),
        signals.continue_without_media,
        sleep,
    )


async def _enter_twin_name(
    page: Page,
    twin_name: str,
    signals: MeetSignals,
    sleep: Callable[[float], Awaitable[None]],
) -> str | None:
    field = page.get_by_placeholder(signals.name_field)
    try:
        found = await field.count()
    except Exception as error:
        return f"finding the name field failed: ({type(error).__name__})"
    if found == 0:
        return f"name field {signals.name_field!r} not found on the lobby page"
    failure = await click_when_ready(
        partial(page.get_by_placeholder, signals.name_field),
        "name field",
        sleep,
        NAME_FIELD_ATTEMPTS,
    )
    if failure is not None:
        return failure
    try:
        await page.keyboard.type(twin_name, delay=TYPE_DELAY_MS)
    except Exception as error:
        return f"typing the twin name failed: ({type(error).__name__})"
    return None


async def _click_join(
    page: Page,
    signals: MeetSignals,
    timeout_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> str | None:
    deadline = monotonic() + timeout_seconds
    while True:
        for label in (signals.ask_to_join, signals.join_now):
            locator = page.locator(f'text="{label}"')
            try:
                found = await locator.count()
            except Exception:
                continue
            if found == 0:
                continue
            failure = await click_when_ready(
                partial(page.locator, f'text="{label}"'),
                label,
                sleep,
            )
            if failure is not None:
                return failure
            return None
        if monotonic() >= deadline:
            return "join button did not become available on the lobby page"
        await sleep(1.0)


@dataclass(frozen=True, slots=True)
class JoinPolling:
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    monotonic: Callable[[], float] = time.monotonic
    abort: Callable[[], bool] | None = None


DEFAULT_JOIN_POLLING = JoinPolling()


async def _prepare_lobby(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals,
    polling: JoinPolling,
    init_scripts: Sequence[str],
) -> str | None:
    failure = await _open_meeting_page(page, request.link, init_scripts)
    if failure is not None:
        return failure
    failure = await _dismiss_media_dialog(page, signals, polling.sleep)
    if failure is not None:
        return failure
    failure = await _enter_twin_name(page, request.twin_name, signals, polling.sleep)
    if failure is not None:
        return failure
    return await _click_join(
        page, signals, request.button_timeout_seconds, polling.sleep, polling.monotonic
    )


async def _poll_content(page: Page, abort: Callable[[], bool] | None) -> str | JoinResult:
    if abort is not None and abort():
        return JoinResult(status="aborted", detail="stop requested while waiting for admission")
    try:
        return await page.content()
    except Exception as error:
        return JoinResult(
            status="failed",
            detail=f"reading lobby page content failed: ({type(error).__name__})",
        )


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
        rechecked = await page.content()
    except Exception:
        return JoinResult(status="timeout", detail=detail)
    if _is_in_call(rechecked, signals):
        return JoinResult(status="admitted", detail="guest admitted to the call")
    return JoinResult(status="timeout", detail=detail)


async def request_guest_join(
    page: Page,
    request: GuestJoinRequest,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
    polling: JoinPolling = DEFAULT_JOIN_POLLING,
    init_scripts: Sequence[str] = (),
) -> JoinResult:
    failure = await _prepare_lobby(page, request, signals, polling, init_scripts)
    if failure is not None:
        return JoinResult(status="failed", detail=failure)
    deadline = polling.monotonic() + request.lobby_timeout_seconds
    captcha_since: float | None = None
    while True:
        content = await _poll_content(page, polling.abort)
        if isinstance(content, JoinResult):
            return content
        if _is_explicit_denial(content):
            return JoinResult(status="denied", detail="guest request to join was denied")
        if signals.denied in content:
            outcome, captcha_since = await _poll_captcha(page, polling, captcha_since, request)
            if outcome is None:
                continue
            return outcome
        if _is_in_call(content, signals):
            return JoinResult(status="admitted", detail="guest admitted to the call")
        if polling.monotonic() >= deadline:
            return await _timeout_outcome(page, content, signals, request)
        await polling.sleep(request.poll_interval_seconds)


async def observe_call_state(
    page: Page,
    signals: MeetSignals = DEFAULT_MEET_SIGNALS,
) -> CallObservation:
    content = await page.content()
    if signals.removed in content:
        return CallObservation(state="removed", detail="guest was removed from the call")
    if signals.ended in content:
        return CallObservation(state="ended", detail="the call has ended")
    if _is_waiting(content):
        return CallObservation(state="lobby", detail="waiting for admission")
    if _is_in_call(content, signals):
        return CallObservation(state="in_call", detail="call controls are visible")
    if signals.ask_to_join in content or signals.join_now in content:
        return CallObservation(state="lobby", detail="lobby controls are visible")
    return CallObservation(state="unknown", detail="no recognized call marker on the page")


async def enable_meeting_microphone(
    page: Page, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> bool:
    enabled = 'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
    disabled = (
        'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
    )
    return await toggle_control(
        page, ToggleSelectors(enabled, disabled), "microphone", "enabled", sleep
    )


async def disable_meeting_camera(
    page: Page, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> bool:
    on = 'button[aria-label^="Turn off camera" i]'
    off = 'button[aria-label^="Turn on camera" i]'
    return await toggle_control(page, ToggleSelectors(off, on), "camera", "disabled", sleep)


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
        if await page.locator(selectors.desired).first.is_visible():
            return True
    except Exception as error:
        raise RuntimeError(f"checking {control} state failed: {error}") from error
    failure = await click_when_ready(
        partial(page.locator, selectors.toggle), f"{control} button", sleep
    )
    if failure is not None:
        raise RuntimeError(failure)
    try:
        await page.locator(selectors.desired).first.wait_for(state="visible", timeout=10000)
    except Exception as error:
        raise RuntimeError(f"waiting for {state} {control} failed: {error}") from error
    return True


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
    except Exception as error:
        return LeaveResult(
            status="failed", detail=f"finding the leave button failed: ({type(error).__name__})"
        )
    if found == 0:
        observation = await observe_call_state(page, signals)
        if observation.state in ("removed", "ended", "lobby"):
            return LeaveResult(
                status="left", detail=f"already out of the call: {observation.state}"
            )
        return LeaveResult(status="failed", detail="leave button not found on the page")
    failure = await click_when_ready(
        partial(page.locator, f'button[aria-label="{signals.leave_call}"]'),
        "leave button",
        sleep,
    )
    if failure is not None:
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
