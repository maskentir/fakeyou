import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from cloakbrowser.human.actionability import ActionabilityError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from fakeyou.meet.meet import (
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
    "modal_observed",
    "watchdog_failed",
]

DIALOG_SELECTOR = '[role="dialog"], [role="alertdialog"], dialog[open]'
_PEOPLE_COUNT = re.compile(r"People\s*[-–:]\s*(\d+)\s+participants?", re.IGNORECASE)
_DIALOG_XPATH = '(//*[@role="dialog" or @role="alertdialog"] | //dialog[@open])'
_SNAPSHOT_ROLE = re.compile(r"^\s*-\s+([a-z]+)")
_INPUT_ROLES = frozenset(
    {
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
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
    buttons: int
    inputs: int

    @property
    def notice(self) -> bool:
        return self.inputs == 0 and self.buttons <= 1


async def _read_modals(page: Page) -> list[_Modal]:
    dialogs = page.locator(DIALOG_SELECTOR)
    result: list[_Modal] = []
    seen: set[str] = set()
    for index in range(min(await dialogs.count(), 32)):
        dialog = dialogs.nth(index)
        if not await dialog.is_visible():
            continue
        raw = await dialog.evaluate("el => el.innerText.slice(0, 4000)", timeout=2000)
        snapshot = await dialog.aria_snapshot(timeout=2000)
        if not isinstance(raw, str) or not isinstance(snapshot, str):
            raise TypeError("Dialog text and structure must be strings")
        roles = [
            match.group(1) for match in map(_SNAPSHOT_ROLE.match, snapshot.splitlines()) if match
        ]
        fingerprint = hashlib.sha256(snapshot.encode()).hexdigest()
        if fingerprint not in seen:
            result.append(
                _Modal(
                    index,
                    " ".join(raw.split()),
                    fingerprint,
                    buttons=roles.count("button"),
                    inputs=sum(role in _INPUT_ROLES for role in roles),
                )
            )
            seen.add(fingerprint)
    return result


def _dialog_button(index: int) -> str:
    return f'xpath={_DIALOG_XPATH}[{index + 1}]//*[self::button or @role="button"]'


async def _current(page: Page, fingerprint: str) -> _Modal | None:
    return next(
        (item for item in await _read_modals(page) if item.fingerprint == fingerprint), None
    )


async def close_modal(page: Page, modal: _Modal) -> bool:
    try:
        current = await _current(page, modal.fingerprint)
        if current is None:
            return True
        target = page.locator(DIALOG_SELECTOR).nth(current.index)
        await target.press("Escape", timeout=2000)
        current = await _current(page, modal.fingerprint)
        if current is None:
            return True
        if current.notice and current.buttons == 1:
            await page.locator(_dialog_button(current.index)).first.click(timeout=2000)
            return await _current(page, modal.fingerprint) is None
    except (PlaywrightError, ActionabilityError):
        return False
    return False


async def dismiss_notices(page: Page) -> tuple[str, ...]:
    dismissed: list[str] = []
    for modal in reversed(await _read_modals(page)):
        if modal.notice and await close_modal(page, modal):
            dismissed.append(modal.text)
    return tuple(dismissed)


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
                    (WatchdogEvent("watchdog_failed", "", "TimeoutError"),),
                    5.0,
                    observation_failed=True,
                )

    def _sweep_expired(self, now: float, events: list[WatchdogEvent]) -> None:
        for fingerprint, deadline in self._manual.items():
            if now >= deadline and fingerprint not in self._expired:
                self._expired.add(fingerprint)
                events.append(
                    WatchdogEvent(
                        "manual_action_expired", fingerprint, "Manual action deadline expired"
                    )
                )

    def _sweep_cleared(self, visible: set[str], events: list[WatchdogEvent]) -> None:
        for fingerprint in self._manual.keys() - visible:
            del self._manual[fingerprint]
            self._expired.discard(fingerprint)
            events.append(
                WatchdogEvent(
                    "manual_action_cleared", fingerprint, "The blocking dialog is no longer visible"
                )
            )

    async def _handle_modal(
        self,
        modal: _Modal,
        observation: CallObservation,
        now: float,
        events: list[WatchdogEvent],
    ) -> None:
        fingerprint = modal.fingerprint
        if self._stable[fingerprint] < 2 or fingerprint in self._manual:
            return
        if modal.inputs and observation.state not in ("in_call", "unknown"):
            self._manual[fingerprint] = now + self._ttl
            events.append(
                WatchdogEvent("manual_action_required", fingerprint, modal.text, self._ttl)
            )
            return
        if fingerprint in self._handled:
            return
        self._handled.add(fingerprint)
        closed = not modal.inputs and await close_modal(self._page, modal)
        events.append(
            WatchdogEvent(
                "modal_dismissed" if closed else "modal_observed", fingerprint, modal.text
            )
        )

    async def _check(self) -> WatchdogCheck:
        events: list[WatchdogEvent] = []
        now = self._monotonic()
        self._sweep_expired(now, events)
        try:
            observation = await self._observe(self._page, self._signals)
        except PlaywrightError as error:
            self._quiet_checks = 0
            self._lobby_detail = None
            self._lobby_checks = 0
            events.append(WatchdogEvent("watchdog_failed", "", type(error).__name__))
            return WatchdogCheck(
                CallObservation("unknown", "UI observation failed"),
                tuple(events),
                5.0,
                observation_failed=True,
            )
        observation = self._debounce_lobby(observation)
        self._last_observation = observation
        try:
            modals = await _read_modals(self._page)
        except (PlaywrightError, TypeError) as error:
            self._quiet_checks = 0
            self._stable.clear()
            events.append(WatchdogEvent("watchdog_failed", "", type(error).__name__))
            return WatchdogCheck(observation, tuple(events), 5.0)
        visible = {modal.fingerprint for modal in modals}
        self._stable = {
            fingerprint: self._stable.get(fingerprint, 0) + 1 for fingerprint in visible
        }
        self._sweep_cleared(visible, events)
        self._handled.intersection_update(visible)
        for modal in modals:
            await self._handle_modal(modal, observation, now, events)
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
    except PlaywrightError:
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
