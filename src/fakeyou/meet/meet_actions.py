import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal

from cloakbrowser.human.actionability import ActionabilityError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from fakeyou.meet.meet import (
    NAME_FIELD_ATTEMPTS,
    ToggleSelectors,
    click_when_ready,
    observe_call_state,
    toggle_control,
)

ActionOutcome = Literal["succeeded", "refused", "failed"]

_XPATH_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_XPATH_LOWER = "abcdefghijklmnopqrstuvwxyz"
_CHAT_FIELD_ATTEMPTS = NAME_FIELD_ATTEMPTS
_PANEL_TIMEOUT_MS = 10000
_ECHO_TIMEOUT_SECONDS = 10.0
_ECHO_POLL_SECONDS = 1.0
_ECHO_READ_TIMEOUT_SECONDS = 2.0
_SETTLE_SECONDS = 3.0
_SETTLE_POLL_SECONDS = 0.5

CHAT_INPUT_SELECTOR = "xpath=//textarea[@maxlength='4000']"


@dataclass(frozen=True, slots=True)
class MeetActionSignals:
    raise_hand: str = "raise hand"
    lower_hand: str = "lower hand"
    chat: str = "chat"
    pin_message: str = "pin message"
    reactions: str = "send a reaction"
    mic_on: str = "turn on microphone"
    mic_off: str = "turn off microphone"
    leave_call: str = "Leave call"


DEFAULT_MEET_ACTION_SIGNALS = MeetActionSignals()


@dataclass(frozen=True, slots=True)
class MeetActionResult:
    outcome: ActionOutcome
    detail: str

    @property
    def succeeded(self) -> bool:
        return self.outcome == "succeeded"

    @property
    def refused(self) -> bool:
        return self.outcome == "refused"

    @property
    def failed(self) -> bool:
        return self.outcome == "failed"


def _button_prefix_selector(prefix: str) -> str:
    lowered = prefix.lower()
    return (
        "xpath=//button[starts-with(translate(@aria-label, "
        f"'{_XPATH_UPPER}', '{_XPATH_LOWER}'), '{lowered}')]"
    )


def _echo_item_selector(signals: MeetActionSignals) -> str:
    return _button_prefix_selector(signals.pin_message)


async def _refused_unless_admitted(page: Page) -> MeetActionResult | None:
    observation = await observe_call_state(page)
    if observation.state == "in_call":
        return None
    return MeetActionResult(
        outcome="refused", detail=f"not admitted to the call: {observation.detail}"
    )


async def _settled_visible(
    page: Page,
    selector: str,
    label: str,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> bool:
    try:
        if await page.locator(selector).first.is_visible():
            return True
    except Exception as error:
        raise RuntimeError(f"checking {label} state failed: {error}") from error
    deadline = monotonic() + _SETTLE_SECONDS
    while monotonic() < deadline:
        await sleep(_SETTLE_POLL_SECONDS)
        try:
            if await page.locator(selector).first.is_visible():
                return True
        except Exception as error:
            raise RuntimeError(f"checking {label} state failed: {error}") from error
    return False


@dataclass(frozen=True, slots=True)
class HandPrefixes:
    desired_prefix: str
    toggle_prefix: str


@dataclass(frozen=True, slots=True)
class ToggleSpec:
    desired_prefix: str
    toggle_prefix: str
    state: str
    label: str


async def _set_toggle_state(
    page: Page,
    spec: ToggleSpec,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> MeetActionResult:
    gate = await _refused_unless_admitted(page)
    if gate is not None:
        return gate
    desired = _button_prefix_selector(spec.desired_prefix)
    toggle = _button_prefix_selector(spec.toggle_prefix)
    if await _settled_visible(page, desired, spec.label, sleep, monotonic):
        return MeetActionResult(outcome="succeeded", detail=f"{spec.label} already {spec.state}")
    await toggle_control(page, ToggleSelectors(desired, toggle), spec.label, spec.state, sleep)
    return MeetActionResult(outcome="succeeded", detail=f"{spec.label} {spec.state}")


async def _set_hand_state(
    page: Page,
    prefixes: HandPrefixes,
    state: str,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> MeetActionResult:
    spec = ToggleSpec(prefixes.desired_prefix, prefixes.toggle_prefix, state, "hand")
    return await _set_toggle_state(page, spec, sleep, monotonic)


async def mute_microphone(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_toggle_state(
        page, ToggleSpec(signals.mic_on, signals.mic_off, "muted", "microphone"), sleep, monotonic
    )


async def unmute_microphone(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_toggle_state(
        page,
        ToggleSpec(signals.mic_off, signals.mic_on, "unmuted", "microphone"),
        sleep,
        monotonic,
    )


async def observe_muted(
    page: Page, signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
) -> bool:
    return await _readback_visible(page, _button_prefix_selector(signals.mic_on))


async def observe_unmuted(
    page: Page, signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
) -> bool:
    return await _readback_visible(page, _button_prefix_selector(signals.mic_off))


_REACTION_CLICK_JS = """emoji => {
    const candidates = document.querySelectorAll('button, [role="button"]');
    for (const el of candidates) {
        const label = el.getAttribute('aria-label') || '';
        const alt = el.querySelector('img')?.alt || '';
        const matches =
            (el.textContent || '').includes(emoji) || label.includes(emoji) || alt.includes(emoji);
        if (matches && el.getClientRects().length > 0) {
            el.click();
            return true;
        }
    }
    return false;
}"""


_HEART_FAMILY = frozenset(
    {
        "\u2764",
        "\u2764\ufe0f",
        "\u2665",
        "\u2665\ufe0f",
        "\U0001f497",
        "\U0001f493",
        "\U0001f495",
        "\U0001f49e",
        "\U0001f498",
        "\U0001f49d",
        "\U0001f9e1",
        "\U0001f49b",
        "\U0001f49a",
        "\U0001f499",
        "\U0001f49c",
        "\U0001f5a4",
        "\U0001f90d",
        "\U0001f90e",
        "\U0001f60d",
    }
)
_MEET_HEART = "\U0001f496"

_EMOJI_FAMILIES: dict[str, str] = (
    {
        member: "\U0001f914"
        for member in (
            "\U0001f615",
            "\U0001f616",
            "\U0001f928",
            "\U0001f9d0",
            "\U0001f636\u200d\U0001f32b\ufe0f",
            "\U0001f62c",
        )
    }
    | {
        member: "\U0001f622"
        for member in (
            "\U0001f62d",
            "\U0001f641",
            "\u2639\ufe0f",
            "\U0001f614",
            "\U0001f61e",
            "\U0001f625",
        )
    }
    | {
        member: "\U0001f62e"
        for member in (
            "\U0001f62f",
            "\U0001f631",
            "\U0001f632",
            "\U0001f92f",
            "\U0001f633",
            "\U0001f62e\u200d\U0001f4a8",
        )
    }
    | {
        member: "\U0001f602"
        for member in (
            "\U0001f923",
            "\U0001f606",
            "\U0001f605",
            "\U0001f604",
            "\U0001f600",
            "\U0001f601",
            "\U0001f60a",
            "\U0001f642",
        )
    }
    | {member: "\U0001f389" for member in ("\U0001f973", "\U0001f38a", "\U0001f525")}
    | {
        member: "\U0001f44f"
        for member in ("\U0001f64c", "\U0001faf6", "\U0001f44f\U0001f3fb", "\U0001f44f\U0001f3fe")
    }
    | {member: "\U0001f44e" for member in ("\U0001f60f", "\U0001f644", "\U0001f612", "\U0001f624")}
)


def _meet_emoji(emoji: str) -> str:
    cleaned = emoji.strip()
    if cleaned in _HEART_FAMILY:
        return _MEET_HEART
    return _EMOJI_FAMILIES.get(cleaned, cleaned)


async def _click_reaction_emoji(
    page: Page, emoji: str, sleep: Callable[[float], Awaitable[None]]
) -> tuple[str | None, str | None]:
    failure = await click_when_ready(
        partial(page.locator, f'button[aria-label="{emoji}"]'), "reaction emoji", sleep
    )
    if failure is None:
        return None, "aria-label"
    failure = await click_when_ready(
        partial(page.locator, f'button:has-text("{emoji}")'), "reaction emoji", sleep
    )
    if failure is None:
        return None, "has-text"
    try:
        clicked = await page.evaluate(_REACTION_CLICK_JS, emoji)
    except (PlaywrightError, ActionabilityError):
        return failure, None
    return (None, "dom-search") if clicked is True else (failure, None)


_REACTION_VISIBLE_JS = """emoji => {
    const candidates = document.querySelectorAll('button, [role="button"]');
    for (const el of candidates) {
        const label = el.getAttribute('aria-label') || '';
        const matches = (el.textContent || '').includes(emoji) || label.includes(emoji);
        if (matches && el.getClientRects().length > 0) return true;
    }
    return false;
}"""

_PICKER_SNIPPET_JS = """() => {
    const root = document.querySelector('[role="dialog"][aria-label="Send a reaction"]');
    if (!root) return 'no picker dialog';
    const parts = [];
    for (const el of root.querySelectorAll('button, [role="button"], img')) {
        const label = el.getAttribute('aria-label') || '';
        const alt = el.getAttribute('alt') || '';
        const text = (el.textContent || '').trim().slice(0, 20);
        const vis = el.getClientRects().length > 0;
        parts.push(el.tagName + '|aria=' + label + '|alt=' + alt + '|text=' + text + '|vis=' + vis);
        if (parts.length >= 16) break;
    }
    return parts.join(' ; ').slice(0, 900);
}"""


async def _emoji_picker_open(page: Page, emoji: str) -> bool:
    try:
        visible = await page.evaluate(_REACTION_VISIBLE_JS, emoji)
    except (PlaywrightError, ActionabilityError):
        return False
    return visible is True


_PICKER_EMOJIS_JS = """() => {
    const labels = [];
    for (const el of document.querySelectorAll('button[aria-label]')) {
        const label = (el.getAttribute('aria-label') || '').trim();
        const pictograph = /\\p{Extended_Pictographic}/u.test(label);
        if (label.length <= 8 && pictograph && el.getClientRects().length > 0) {
            labels.push(label);
        }
    }
    return labels;
}"""


async def _picker_emojis(page: Page) -> list[str]:
    try:
        raw = await page.evaluate(_PICKER_EMOJIS_JS)
    except (PlaywrightError, ActionabilityError):
        return []
    return [str(item) for item in raw] if isinstance(raw, list) else []


async def _picker_snippet(page: Page) -> str:
    try:
        raw = await page.evaluate(_PICKER_SNIPPET_JS)
    except (PlaywrightError, ActionabilityError):
        return ""
    return " ".join(str(raw).split()) if raw else ""


async def send_reaction(
    page: Page,
    emoji: str,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    del monotonic
    gate = await _refused_unless_admitted(page)
    if gate is not None:
        return gate
    emoji = _meet_emoji(emoji)
    picker = partial(page.locator, _button_prefix_selector(signals.reactions))
    if not await _emoji_picker_open(page, emoji):
        failure = await click_when_ready(picker, "reactions button", sleep)
        if failure is not None:
            return MeetActionResult(
                outcome="failed", detail=f"opening the reaction picker failed: {failure}"
            )
    try:
        available = await _picker_emojis(page)
        if available and emoji not in available:
            return MeetActionResult(
                outcome="failed",
                detail=f"Meet does not offer that reaction (available: {' '.join(available)})",
            )
        failure, strategy = await _click_reaction_emoji(page, emoji, sleep)
        if failure is not None and not available:
            await click_when_ready(picker, "reactions button", sleep)
            failure, strategy = await _click_reaction_emoji(page, emoji, sleep)
        if failure is not None:
            snippet = await _picker_snippet(page)
            detail = f"picking the reaction failed: {failure}"
            if snippet:
                detail = f"{detail}; picker candidates: {snippet}"
            return MeetActionResult(outcome="failed", detail=detail)
        return MeetActionResult(outcome="succeeded", detail=f"reaction sent via {strategy}")
    finally:
        try:
            await page.keyboard.press("Escape")
        except (PlaywrightError, ActionabilityError):
            pass


async def leave_call(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    del monotonic
    gate = await _refused_unless_admitted(page)
    if gate is not None:
        return gate
    failure = await click_when_ready(
        partial(page.locator, f'button[aria-label="{signals.leave_call}"]'),
        "leave button",
        sleep,
    )
    if failure is not None:
        return MeetActionResult(outcome="failed", detail=f"clicking leave failed: {failure}")
    return MeetActionResult(outcome="succeeded", detail="leave clicked")


async def raise_hand(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_hand_state(
        page, HandPrefixes(signals.lower_hand, signals.raise_hand), "raised", sleep, monotonic
    )


async def lower_hand(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_hand_state(
        page, HandPrefixes(signals.raise_hand, signals.lower_hand), "lowered", sleep, monotonic
    )


async def _await_own_echo(
    page: Page,
    text: str,
    signals: MeetActionSignals,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> bool:
    echo_item = _echo_item_selector(signals)
    deadline = monotonic() + _ECHO_TIMEOUT_SECONDS
    while True:
        try:
            async with asyncio.timeout(_ECHO_READ_TIMEOUT_SECONDS):
                content = await page.content()
        except TimeoutError:
            content = ""
        if text in content:
            try:
                found = await page.locator(echo_item).count()
            except PlaywrightError:
                found = 0
            if found > 0:
                return True
        if monotonic() >= deadline:
            return False
        await sleep(_ECHO_POLL_SECONDS)


async def send_chat(
    page: Page,
    text: str,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    if not text.strip():
        raise ValueError("chat text must not be empty")
    gate = await _refused_unless_admitted(page)
    if gate is not None:
        return gate
    chars = len(text)
    if not await _settled_visible(page, CHAT_INPUT_SELECTOR, "chat input", sleep, monotonic):
        failure = await click_when_ready(
            partial(page.locator, _button_prefix_selector(signals.chat)),
            "chat button",
            sleep,
        )
        if failure is not None:
            return MeetActionResult(
                outcome="failed",
                detail=f"opening the chat panel failed: {failure} (chars={chars})",
            )
        try:
            await page.locator(CHAT_INPUT_SELECTOR).first.wait_for(
                state="visible", timeout=_PANEL_TIMEOUT_MS
            )
        except PlaywrightError as error:
            return MeetActionResult(
                outcome="failed",
                detail=f"chat input never appeared: {type(error).__name__} (chars={chars})",
            )
    failure = await click_when_ready(
        partial(page.locator, CHAT_INPUT_SELECTOR),
        "chat input",
        sleep,
        _CHAT_FIELD_ATTEMPTS,
    )
    if failure is not None:
        return MeetActionResult(
            outcome="failed",
            detail=f"focusing the chat input failed: {failure} (chars={chars})",
        )
    try:
        await page.keyboard.insert_text(text)
        await page.keyboard.press("Enter")
    except PlaywrightError as error:
        return MeetActionResult(
            outcome="failed",
            detail=f"typing the chat message failed: {type(error).__name__} (chars={chars})",
        )
    if await _await_own_echo(page, text, signals, sleep, monotonic):
        return MeetActionResult(
            outcome="succeeded", detail=f"own chat echo observed (chars={chars})"
        )
    return MeetActionResult(
        outcome="failed",
        detail=f"own chat echo not observed within {_ECHO_TIMEOUT_SECONDS} seconds (chars={chars})",
    )


async def _readback_visible(page: Page, selector: str) -> bool:
    try:
        return await page.locator(selector).first.is_visible()
    except PlaywrightError:
        return False


async def observe_hand_raised(
    page: Page, signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
) -> bool:
    return await _readback_visible(page, _button_prefix_selector(signals.lower_hand))


async def observe_hand_lowered(
    page: Page, signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
) -> bool:
    return await _readback_visible(page, _button_prefix_selector(signals.raise_hand))


async def observe_chat_echoed(
    page: Page, text: str, signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS
) -> bool:
    try:
        async with asyncio.timeout(_ECHO_READ_TIMEOUT_SECONDS):
            content = await page.content()
    except TimeoutError:
        return False
    if text not in content:
        return False
    return await _readback_visible(page, _echo_item_selector(signals))
