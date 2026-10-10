import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Literal

from playwright.async_api import Page

from fakeyou.meet.meet import (
    NAME_FIELD_ATTEMPTS,
    TYPE_DELAY_MS,
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
_SETTLE_SECONDS = 3.0
_SETTLE_POLL_SECONDS = 0.5

CHAT_INPUT_SELECTOR = "xpath=//textarea[@maxlength='4000']"


@dataclass(frozen=True, slots=True)
class MeetActionSignals:
    raise_hand: str = "raise hand"
    lower_hand: str = "lower hand"
    chat: str = "chat"
    pin_message: str = "pin message"


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


async def _set_hand_state(
    page: Page,
    desired_prefix: str,
    toggle_prefix: str,
    state: str,
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> MeetActionResult:
    gate = await _refused_unless_admitted(page)
    if gate is not None:
        return gate
    desired = _button_prefix_selector(desired_prefix)
    toggle = _button_prefix_selector(toggle_prefix)
    if await _settled_visible(page, desired, "hand", sleep, monotonic):
        return MeetActionResult(outcome="succeeded", detail=f"hand already {state}")
    await toggle_control(page, desired, toggle, "hand", state, sleep)
    return MeetActionResult(outcome="succeeded", detail=f"hand {state}")


async def raise_hand(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_hand_state(
        page, signals.lower_hand, signals.raise_hand, "raised", sleep, monotonic
    )


async def lower_hand(
    page: Page,
    signals: MeetActionSignals = DEFAULT_MEET_ACTION_SIGNALS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> MeetActionResult:
    return await _set_hand_state(
        page, signals.raise_hand, signals.lower_hand, "lowered", sleep, monotonic
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
        content = await page.content()
        if text in content:
            try:
                found = await page.locator(echo_item).count()
            except Exception:
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
        except Exception as error:
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
        await page.keyboard.type(text, delay=TYPE_DELAY_MS)
        await page.keyboard.press("Enter")
    except Exception as error:
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
