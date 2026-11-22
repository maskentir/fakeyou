import asyncio
import time
from typing import Any, override

import pytest
from playwright.async_api import Error as PlaywrightError

import fakeyou.meet.meet as meet
from fakeyou.meet.meet import (
    CALL_CONTROLS_TIMEOUT_SECONDS,
    MIC_DISABLED_SELECTOR,
    MIC_ENABLED_SELECTOR,
    PROBE_TIMEOUT_SECONDS,
    ControlsClock,
    wait_for_mic_control,
)
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import LEAVE_SELECTOR, FakeLocator, FakePage

LEAK = "Harun Rasyid ai2@example.com abc-defg-hij"
TOOLBAR_PROBES = (MIC_ENABLED_SELECTOR, MIC_DISABLED_SELECTOR, LEAVE_SELECTOR)


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

    @override
    async def is_visible(self) -> bool:
        page = self._probe_page
        page.probes.append((self.selector, page.fake_clock.now))
        behaviour = page.behaviour.get(self.selector)
        if behaviour == "hang":
            await asyncio.Event().wait()
        if behaviour == "raise":
            raise PlaywrightError(LEAK)
        if behaviour == "stop":
            page.fake_clock.stop_at = page.fake_clock.now
            return True
        return page.fake_clock.now >= page.appears.get(self.selector, float("inf"))


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


async def test_t11_polling_never_probes_or_sleeps_past_the_deadline() -> None:
    clock = BoundedClock()
    page = ProbePage(clock, {MIC_DISABLED_SELECTOR: 19.5, LEAVE_SELECTOR: 0.0})

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
