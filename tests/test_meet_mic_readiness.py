import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, override

import cloakbrowser.human as humanized
import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

import fakeyou.meet.meet as meet
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import (
    CALL_CONTROLS_TIMEOUT_SECONDS,
    MIC_CONTROL_SELECTOR,
    MIC_DISABLED_SELECTOR,
    MIC_ENABLED_SELECTOR,
    PROBE_TIMEOUT_SECONDS,
    ControlsClock,
    enable_meeting_microphone,
    wait_for_mic_control,
)
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import LEAVE_SELECTOR, FakeLocator, FakePage
from tests._meet_support import FIXTURE_URL

LEAK = "Harun Rasyid ai2@example.com abc-defg-hij"
MIC_STATES = (MIC_ENABLED_SELECTOR, MIC_DISABLED_SELECTOR)
TOOLBAR_PROBES = (*MIC_STATES, LEAVE_SELECTOR)


class BoundedClock(FakeClock):
    def __init__(self) -> None:
        super().__init__(now=0.0)
        self.stop_at: float | None = None

    @override
    async def sleep(self, seconds: float) -> None:
        assert seconds > 0
        assert len(self.sleeps) < 100
        await super().sleep(seconds)

    def stopped(self) -> bool:
        return self.stop_at is not None and self.now >= self.stop_at


class ProbeLocator(FakeLocator):
    def __init__(self, page: "ProbePage", selector: str) -> None:
        super().__init__(page, selector)
        self._probe_page = page

    def _parts(self) -> tuple[str, ...]:
        return MIC_STATES if self.selector == MIC_CONTROL_SELECTOR else (self.selector,)

    async def _observe(self) -> bool:
        page = self._probe_page
        page.probes.append((self.selector, page.fake_clock.now))
        behaviours = [page.behaviour[part] for part in self._parts() if part in page.behaviour]
        behaviour = behaviours[0] if behaviours else None
        if behaviour == "hang":
            await asyncio.Event().wait()
        if behaviour == "raise":
            raise PlaywrightError(LEAK)
        if behaviour == "stop":
            page.fake_clock.stop_at = page.fake_clock.now
            return True
        return page.fake_clock.now >= self._appears()

    def _appears(self) -> float:
        return min(self._probe_page.appears.get(part, float("inf")) for part in self._parts())

    @override
    async def is_visible(self) -> bool:
        return await self._observe()

    @override
    async def wait_for(self, **kwargs: Any) -> None:
        assert kwargs.get("state") == "visible"
        if await self._observe():
            return
        clock = self._probe_page.fake_clock
        window = float(kwargs["timeout"]) / 1000
        appears = self._appears()
        await clock.sleep(min(window, appears - clock.now))
        if clock.now < appears:
            raise PlaywrightTimeoutError(LEAK)


class ProbePage(FakePage):
    def __init__(
        self,
        clock: BoundedClock,
        appears: dict[str, float] | None = None,
        behaviour: dict[str, str] | None = None,
    ) -> None:
        super().__init__([], [])
        self.fake_clock = clock
        self.appears = appears or {}
        self.behaviour = behaviour or {}
        self.probes: list[tuple[str, float]] = []

    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        del kwargs
        return ProbeLocator(self, selector)


def _clock(
    clock: BoundedClock, timeout_seconds: float = CALL_CONTROLS_TIMEOUT_SECONDS
) -> ControlsClock:
    return ControlsClock(clock.sleep, clock.monotonic, clock.stopped, 2.0, timeout_seconds)


def test_t7_readiness_bounds_and_mic_selectors_are_fixed() -> None:
    assert CALL_CONTROLS_TIMEOUT_SECONDS == 20.0
    assert PROBE_TIMEOUT_SECONDS == 2.0
    assert ControlsClock(asyncio.sleep, time.monotonic, bool, 2.0).timeout_seconds == 20.0
    assert MIC_ENABLED_SELECTOR == (
        'button[aria-label^="Turn off microphone" i],button[aria-label^="Mute microphone" i]'
    )
    assert MIC_DISABLED_SELECTOR == (
        'button[aria-label^="Turn on microphone" i],button[aria-label^="Unmute microphone" i]'
    )
    assert MIC_CONTROL_SELECTOR == f"{MIC_ENABLED_SELECTOR},{MIC_DISABLED_SELECTOR}"


async def test_t7_the_deadline_is_the_controls_timeout_from_the_wait_start() -> None:
    clock = BoundedClock()
    clock.now = 50.0

    wait = await wait_for_mic_control(ProbePage(clock), _clock(clock))

    assert wait.outcome == "toolbar_missing"
    assert wait.waited_seconds == 20.0
    assert clock.now == 70.0


@pytest.mark.parametrize("selector", [MIC_ENABLED_SELECTOR, MIC_DISABLED_SELECTOR])
async def test_either_mic_state_is_readiness(selector: str) -> None:
    clock = BoundedClock()

    wait = await wait_for_mic_control(ProbePage(clock, {selector: 6.0}), _clock(clock))

    assert (wait.outcome, wait.waited_seconds) == ("ready", 6.0)


async def test_leave_alone_is_reported_as_a_missing_mic_control() -> None:
    clock = BoundedClock()

    wait = await wait_for_mic_control(ProbePage(clock, {LEAVE_SELECTOR: 0.0}), _clock(clock))

    assert wait.outcome == "mic_control_missing"


async def test_t9_a_stop_observed_with_the_deadline_returns_stopped() -> None:
    clock = BoundedClock()
    clock.stop_at = 20.0

    wait = await wait_for_mic_control(ProbePage(clock, {LEAVE_SELECTOR: 0.0}), _clock(clock))

    assert wait.outcome == "stopped"


async def test_t9_a_stop_before_a_visible_mic_is_reported_is_not_readiness() -> None:
    clock = BoundedClock()
    clock.stop_at = 0.0

    wait = await wait_for_mic_control(ProbePage(clock, {MIC_DISABLED_SELECTOR: 0.0}), _clock(clock))

    assert wait.outcome == "stopped"


async def test_t9_a_stop_during_the_successful_probe_is_not_readiness() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, behaviour={MIC_ENABLED_SELECTOR: "stop"})

    wait = await wait_for_mic_control(page, _clock(clock))

    assert wait.outcome == "stopped"


async def test_t10_a_hanging_probe_is_bounded_and_counts_as_not_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meet, "PROBE_TIMEOUT_SECONDS", 0.01)
    clock = BoundedClock()
    page = ProbePage(
        clock,
        {LEAVE_SELECTOR: 0.0},
        {MIC_ENABLED_SELECTOR: "hang", MIC_DISABLED_SELECTOR: "hang"},
    )

    async with asyncio.timeout(5):
        wait = await wait_for_mic_control(page, _clock(clock))

    assert wait.outcome == "mic_control_missing"


async def test_t10_a_probe_is_bounded_by_the_remaining_deadline() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, behaviour=dict.fromkeys(TOOLBAR_PROBES, "hang"))

    started = time.monotonic()
    async with asyncio.timeout(5):
        wait = await wait_for_mic_control(page, _clock(clock, timeout_seconds=0.05))

    assert wait.outcome == "toolbar_missing"
    assert time.monotonic() - started < 1.0


async def test_t10_probe_errors_count_as_not_visible_and_never_leak_page_text() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, behaviour=dict.fromkeys(TOOLBAR_PROBES, "raise"))

    wait = await wait_for_mic_control(page, _clock(clock))

    assert wait.outcome == "toolbar_missing"
    assert LEAK not in repr(wait)


async def test_t11_a_mic_in_the_last_window_before_the_deadline_is_readiness() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, {MIC_DISABLED_SELECTOR: 19.5, LEAVE_SELECTOR: 0.0})

    wait = await wait_for_mic_control(page, _clock(clock))

    assert (wait.outcome, wait.waited_seconds) == ("ready", 19.5)


async def test_readiness_is_reported_when_the_mic_appears_not_at_the_next_poll() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, {MIC_ENABLED_SELECTOR: 13.25})

    wait = await wait_for_mic_control(page, _clock(clock))

    assert (wait.outcome, wait.waited_seconds) == ("ready", 13.25)


async def test_t11_a_mic_after_the_deadline_is_never_observed() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, {MIC_DISABLED_SELECTOR: 20.5, LEAVE_SELECTOR: 0.0})

    wait = await wait_for_mic_control(page, _clock(clock))

    assert wait.outcome == "mic_control_missing"
    assert max(at for _, at in page.probes) < 20.0
    assert sum(clock.sleeps) == 20.0
    assert clock.now == 20.0


async def test_t11_the_last_sleep_is_clipped_to_the_deadline() -> None:
    clock = BoundedClock()

    await wait_for_mic_control(ProbePage(clock), _clock(clock, timeout_seconds=5.0))

    assert clock.sleeps == [2.0, 2.0, 1.0]


async def test_the_controls_timeout_reports_only_a_heading_category() -> None:
    clock = BoundedClock()
    page = ProbePage(clock)
    page.labels = {"h1, h2": [f"Getting ready for {LEAK}"]}

    wait = await wait_for_mic_control(page, _clock(clock))

    assert wait.heading == "getting ready"


def _late_button_page(label: str, after_ms: int) -> str:
    return (
        '<button aria-label="Leave call"></button><script>setTimeout(() => {'
        "const button = document.createElement('button');"
        f"button.setAttribute('aria-label', '{label}');"
        "document.body.appendChild(button);"
        f"}}, {after_ms});</script>"
    )


MEET_SIZED_CONTROLS = "<style>button { width: 48px; height: 48px; }</style>"


@asynccontextmanager
async def _fixture_page(html: str) -> AsyncIterator[Page]:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        body = MEET_SIZED_CONTROLS + html
        await page.route(
            FIXTURE_URL, lambda route: route.fulfill(body=body, content_type="text/html")
        )
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        yield page


async def _real_wait(html: str, monkeypatch: pytest.MonkeyPatch) -> tuple[str, float]:
    monkeypatch.setattr(meet, "PROBE_TIMEOUT_SECONDS", 0.5)
    async with _fixture_page(html) as page:
        clock = ControlsClock(asyncio.sleep, time.monotonic, lambda: False, 0.5, 2.0)
        started = time.monotonic()
        async with asyncio.timeout(5):
            wait = await wait_for_mic_control(page, clock)
        return wait.outcome, time.monotonic() - started


@pytest.mark.browser
async def test_a_real_mic_control_in_the_last_window_is_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, elapsed = await _real_wait(_late_button_page("Turn on microphone", 1750), monkeypatch)

    assert outcome == "ready"
    assert 1.5 < elapsed < 2.0


@pytest.mark.browser
async def test_a_real_mic_control_after_the_deadline_is_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, elapsed = await _real_wait(_late_button_page("Turn on microphone", 2600), monkeypatch)

    assert outcome == "mic_control_missing"
    assert 2.0 <= elapsed < 2.5


TURN_ON = "Turn on microphone"
TURN_OFF = "Turn off microphone"


@dataclass(frozen=True, slots=True)
class MicDuplicate:
    hidden: str
    visible: str | None
    visible_first: bool = False


def _duplicate_page(case: MicDuplicate) -> str:
    hidden = f'<button aria-label="{case.hidden}" style="display:none"></button>'
    if case.visible is None:
        return f'<button aria-label="Leave call"></button>{hidden}'
    insert = "prepend" if case.visible_first else "append"
    return (
        f'<button aria-label="Leave call"></button><div id="bar">{hidden}</div>'
        "<script>setTimeout(() => {const button = document.createElement('button');"
        f"button.setAttribute('aria-label', '{case.visible}');"
        f"button.onclick = () => button.setAttribute('aria-label', '{TURN_OFF}');"
        f"document.getElementById('bar').{insert}(button);}}, 300);</script>"
    )


async def _real_readiness_and_enable(
    case: MicDuplicate, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, bool | str]:
    monkeypatch.setattr(meet, "PROBE_TIMEOUT_SECONDS", 0.5)
    async with _fixture_page(_duplicate_page(case)) as page:
        clock = ControlsClock(asyncio.sleep, time.monotonic, lambda: False, 0.5, 2.0)
        async with asyncio.timeout(5):
            wait = await wait_for_mic_control(page, clock)
        if wait.outcome != "ready":
            return wait.outcome, False
        return wait.outcome, await _enabled(page)


async def _enabled(page: Page) -> bool | str:
    try:
        async with asyncio.timeout(30):
            return await enable_meeting_microphone(page)
    except RuntimeError as error:
        return f"{type(error).__name__}: {error}"


@pytest.mark.browser
@pytest.mark.parametrize(
    "case",
    [
        MicDuplicate(hidden=TURN_OFF, visible=TURN_ON),
        MicDuplicate(hidden=TURN_ON, visible=TURN_ON),
        MicDuplicate(hidden=TURN_OFF, visible=TURN_ON, visible_first=True),
        MicDuplicate(hidden=TURN_ON, visible=TURN_ON, visible_first=True),
    ],
    ids=["a-hidden-enabled-first", "b-hidden-disabled-first", "c-reverse-a", "c-reverse-b"],
)
async def test_a_visible_mic_control_wins_over_hidden_duplicates_in_any_order(
    case: MicDuplicate, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, enabled = await _real_readiness_and_enable(case, monkeypatch)

    assert (outcome, enabled) == ("ready", True)


@pytest.mark.browser
@pytest.mark.parametrize("hidden", [TURN_ON, TURN_OFF])
async def test_d_only_hidden_mic_controls_time_out_at_the_deadline(
    hidden: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, elapsed = await _real_wait(_duplicate_page(MicDuplicate(hidden, None)), monkeypatch)

    assert outcome == "mic_control_missing"
    assert 2.0 <= elapsed < 2.5


@pytest.mark.browser
@pytest.mark.parametrize(
    "case",
    [
        MicDuplicate(hidden=TURN_OFF, visible=TURN_ON),
        MicDuplicate(hidden=TURN_ON, visible=TURN_ON),
        MicDuplicate(hidden=TURN_OFF, visible=TURN_ON, visible_first=True),
        MicDuplicate(hidden=TURN_ON, visible=TURN_ON, visible_first=True),
    ],
    ids=["a-hidden-enabled-first", "b-hidden-disabled-first", "c-reverse-a", "c-reverse-b"],
)
async def test_enabling_the_mic_clicks_the_visible_control_among_hidden_duplicates(
    case: MicDuplicate,
) -> None:
    async with _fixture_page(_duplicate_page(case)) as page:
        await page.get_by_role("button", name=case.visible).last.wait_for(state="attached")
        enabled = await _enabled(page)
        visible_labels = await page.locator("button:visible").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('aria-label'))"
        )

    assert enabled is True
    assert TURN_OFF in visible_labels


async def _on_static_page(html: str, check: Callable[[Page], Awaitable[None]]) -> None:
    async with _fixture_page(html) as page:
        await check(page)


@pytest.mark.browser
async def test_an_enabled_mic_behind_a_hidden_duplicate_needs_no_click() -> None:
    html = (
        f'<button aria-label="{TURN_OFF}" style="display:none"></button>'
        f'<button aria-label="{TURN_OFF}" onclick="this.dataset.clicked = 1"></button>'
    )

    async def check(page: Page) -> None:
        assert await enable_meeting_microphone(page) is True
        assert await page.locator("[data-clicked]").count() == 0

    await _on_static_page(html, check)


@pytest.mark.browser
async def test_a_visible_leave_behind_a_hidden_duplicate_is_toolbar_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(meet, "PROBE_TIMEOUT_SECONDS", 0.5)
    html = '<button aria-label="Leave call" style="display:none"></button>' + (
        '<button aria-label="Leave call"></button>'
    )

    async def check(page: Page) -> None:
        clock = ControlsClock(asyncio.sleep, time.monotonic, lambda: False, 0.5, 1.0)
        async with asyncio.timeout(5):
            wait = await wait_for_mic_control(page, clock)
        assert wait.outcome == "mic_control_missing"

    await _on_static_page(html, check)


def _lowest_click_target(box: dict[str, float], *_aim: object) -> SimpleNamespace:
    return SimpleNamespace(
        x=round(box["x"] + box["width"] * 0.5), y=round(box["y"] + box["height"] * 0.65)
    )


@pytest.mark.browser
async def test_fixture_mic_controls_survive_the_worst_humanized_click_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = vars(humanized)["async_human_move"]

    async def move_then_max_jitter(raw: Any, *path: Any) -> None:
        await move(raw, *path)
        _start_x, _start_y, end_x, end_y, _cfg = path
        await raw.move(round(end_x), round(end_y + 2))

    monkeypatch.setattr(humanized, "click_target", _lowest_click_target)
    monkeypatch.setattr(humanized, "async_human_move", move_then_max_jitter)
    case = MicDuplicate(hidden=TURN_ON, visible=TURN_ON, visible_first=True)

    async with _fixture_page(_duplicate_page(case)) as page:
        await page.get_by_role("button", name=TURN_ON).last.wait_for(state="attached")
        enabled = await _enabled(page)

    assert enabled is True
