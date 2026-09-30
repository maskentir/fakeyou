import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from playwright.async_api import Page

from fakeyou.meet import (
    DEFAULT_MEET_SIGNALS,
    CallObservation,
    MeetSignals,
    observe_call_state,
)

WatchdogEventKind = Literal[
    "manual_action_required",
    "manual_action_cleared",
    "manual_action_expired",
    "modal_dismissed",
    "watchdog_error",
]

_DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], dialog[open]'
_PEOPLE_COUNT = re.compile(r"People\s*[-–:]\s*(\d+)\s+participants?", re.IGNORECASE)
_DIALOG_XPATH = '(//*[@role="dialog" or @role="alertdialog"] | //dialog[@open])'


def _dialog_button_xpath(index: int, label: str) -> str:
    return (
        f'xpath={_DIALOG_XPATH}[{index + 1}]//*[(self::button or @role="button") and '
        f'(@aria-label="{label}" or normalize-space(.)="{label}")]'
    )


_HARMLESS_NOTICES = frozenset(
    {
        ("this meeting is being recorded got it", "Got it"),
        ("this meeting is being transcribed got it", "Got it"),
    }
)
_DISMISSABLE_ERRORS = frozenset(
    {
        (
            "close close error camera not found make sure your camera is plugged in",
            "Close",
        ),
    }
)


@dataclass(frozen=True, slots=True)
class WatchdogEvent:
    kind: WatchdogEventKind
    fingerprint: str
    detail: str
    ttl_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class WatchdogCheck:
    observation: CallObservation
    events: tuple[WatchdogEvent, ...]
    next_check_seconds: float
    participant_count: int | None = None
    observation_failed: bool = False


@dataclass(frozen=True, slots=True)
class _Modal:
    index: int
    text: str
    fingerprint: str


class UIWatchdog:
    def __init__(
        self,
        page: Page,
        *,
        signals: MeetSignals = DEFAULT_MEET_SIGNALS,
        observe: Callable[[Page, MeetSignals], Awaitable[CallObservation]] = observe_call_state,
        monotonic: Callable[[], float] = time.monotonic,
        manual_action_ttl_seconds: float = 180.0,
    ) -> None:
        if manual_action_ttl_seconds <= 0:
            raise ValueError("manual_action_ttl_seconds must be positive")
        self._page = page
        self._signals = signals
        self._observe = observe
        self._monotonic = monotonic
        self._ttl = manual_action_ttl_seconds
        self._quiet_checks = 0
        self._lobby_detail: str | None = None
        self._lobby_checks = 0
        self._handled: set[str] = set()
        self._manual: dict[str, float] = {}
        self._expired: set[str] = set()
        self._lock = asyncio.Lock()
        self._stable: dict[str, int] = {}
        self._last_observation = CallObservation("unknown", "UI has not been observed")

    async def check(self) -> WatchdogCheck:
        async with self._lock:
            try:
                async with asyncio.timeout(5):
                    return await self._check()
            except TimeoutError:
                self._quiet_checks = 0
                return WatchdogCheck(
                    self._last_observation,
                    (WatchdogEvent("watchdog_error", "", "TimeoutError"),),
                    5.0,
                )

    async def _check(self) -> WatchdogCheck:
        events: list[WatchdogEvent] = []
        now = self._monotonic()
        for fingerprint, deadline in self._manual.items():
            if now >= deadline and fingerprint not in self._expired:
                self._expired.add(fingerprint)
                events.append(
                    WatchdogEvent(
                        "manual_action_expired", fingerprint, "Manual action deadline expired"
                    )
                )
        try:
            observation = await self._observe(self._page, self._signals)
        except Exception as error:
            self._quiet_checks = 0
            self._lobby_detail = None
            self._lobby_checks = 0
            events.append(WatchdogEvent("watchdog_error", "", type(error).__name__))
            return WatchdogCheck(
                CallObservation("unknown", "UI observation failed"),
                tuple(events),
                5.0,
                observation_failed=True,
            )
        observation = self._debounce_lobby(observation)
        self._last_observation = observation
        try:
            modals = await self._read_modals()
        except Exception as error:
            self._quiet_checks = 0
            self._stable.clear()
            events.append(WatchdogEvent("watchdog_error", "", type(error).__name__))
            return WatchdogCheck(observation, tuple(events), 5.0)
        visible = {modal.fingerprint for modal in modals}
        self._stable = {
            fingerprint: self._stable.get(fingerprint, 0) + 1 for fingerprint in visible
        }
        for fingerprint in self._manual.keys() - visible:
            del self._manual[fingerprint]
            self._expired.discard(fingerprint)
            events.append(
                WatchdogEvent(
                    "manual_action_cleared", fingerprint, "The blocking dialog is no longer visible"
                )
            )
        self._handled.intersection_update(visible)
        for modal in modals:
            if modal.fingerprint in self._handled or self._stable[modal.fingerprint] < 2:
                continue
            dismissed = False
            action = next(
                (label for text, label in _HARMLESS_NOTICES if text == modal.text.casefold()), None
            )
            strict_buttons = action is not None
            if action is None:
                action = next(
                    (label for text, label in _DISMISSABLE_ERRORS if text == modal.text.casefold()),
                    None,
                )
            if action is not None:
                try:
                    target = self._page.locator(_DIALOG_SELECTOR).nth(modal.index)
                    current = await target.evaluate(
                        "el => el.innerText.slice(0, 4000)", timeout=2000
                    )
                    single_button = (
                        await target.get_by_role("button").count() == 1 if strict_buttons else True
                    )
                    if (
                        isinstance(current, str)
                        and " ".join(current.split()) == modal.text
                        and single_button
                    ):
                        await self._page.locator(
                            _dialog_button_xpath(modal.index, action)
                        ).first.click(timeout=2000)
                        dismissed = True
                except Exception as error:
                    events.append(
                        WatchdogEvent("watchdog_error", modal.fingerprint, type(error).__name__)
                    )
            self._handled.add(modal.fingerprint)
            if dismissed:
                events.append(WatchdogEvent("modal_dismissed", modal.fingerprint, modal.text))
            else:
                self._manual[modal.fingerprint] = now + self._ttl
                events.append(
                    WatchdogEvent(
                        "manual_action_required", modal.fingerprint, modal.text, self._ttl
                    )
                )
        self._quiet_checks = 0 if modals else self._quiet_checks + 1
        delay = 20.0 if self._quiet_checks >= 3 else 5.0
        count = await read_participant_count(self._page) if observation.state == "in_call" else None
        return WatchdogCheck(observation, tuple(events), delay, count)

    def _debounce_lobby(self, observation: CallObservation) -> CallObservation:
        if observation.state != "lobby":
            self._lobby_detail = None
            self._lobby_checks = 0
            return observation
        if observation.detail == self._lobby_detail:
            self._lobby_checks += 1
        else:
            self._lobby_detail = observation.detail
            self._lobby_checks = 1
        if self._lobby_checks < 2:
            return CallObservation("unknown", "Waiting to confirm the lobby observation")
        return observation

    async def _read_modals(self) -> list[_Modal]:
        dialogs = self._page.locator(_DIALOG_SELECTOR)
        result: list[_Modal] = []
        seen: set[str] = set()
        for index in range(min(await dialogs.count(), 32)):
            dialog = dialogs.nth(index)
            if not await dialog.is_visible():
                continue
            raw = await dialog.evaluate("el => el.innerText.slice(0, 4000)", timeout=2000)
            if not isinstance(raw, str):
                raise TypeError("Dialog text must be a string")
            text = " ".join(raw.split())
            fingerprint = hashlib.sha256(text.encode()).hexdigest()
            if fingerprint not in seen:
                result.append(_Modal(index, text, fingerprint))
                seen.add(fingerprint)
        return result


async def read_participant_count(page: Page) -> int | None:
    try:
        labels = await page.get_by_role("button").evaluate_all(
            """buttons => buttons.slice(0, 256).filter(el => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0
                    && style.visibility !== 'hidden' && style.display !== 'none';
            }).map(el => (el.getAttribute('aria-label') || '').slice(0, 200))"""
        )
    except Exception:
        return None
    if not isinstance(labels, list):
        return None
    counts: set[int] = set()
    for label in labels:
        if isinstance(label, str) and (match := _PEOPLE_COUNT.fullmatch(label.strip())):
            counts.add(int(match.group(1)))
    if len(counts) != 1:
        return None
    count = counts.pop()
    return count if count > 0 else None
