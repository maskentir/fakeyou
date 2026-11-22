from typing import Any, override

import pytest
from playwright.async_api import Error as PlaywrightError

from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import (
    CONTROL_LABEL_LIMIT,
    GuestJoinRequest,
    JoinPolling,
    control_category,
    enable_meeting_microphone,
    leave_meeting,
    request_guest_join,
    visible_control_categories,
)
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import (
    DENIED,
    LEAVE_SELECTOR,
    MIC_OFF_SELECTOR,
    MIC_ON_SELECTOR,
    NAME_SELECTOR,
    NO_CAPTCHA,
    FakeLocator,
    FakePage,
    FakePageSession,
    meet_page,
)
from tests._meet_support import FIXTURE_URL
from tests._persistence_fakes import RecordingJournal
from tests._runtime_support import session_config

SENSITIVE = (
    "Rani Wijaya",
    "rani.wijaya@example.com",
    "xqz-mnop-rst",
    "48213-SECRET-7731",
)
LABELS = {
    "button[aria-label]": [
        "Leave call",
        "Turn on microphone",
        "Pin Rani Wijaya's tile",
        "More options for rani.wijaya@example.com",
        "xqz-mnop-rst",
        "Copy passcode 48213-SECRET-7731",
    ]
}
LEAK = 'locator resolved to <button aria-label="Pin Rani Wijaya\'s tile"> xqz-mnop-rst'


def _leaked(*written: str) -> list[str]:
    text = "\n".join(written)
    return [part for part in SENSITIVE if part in text]


def _request() -> GuestJoinRequest:
    return GuestJoinRequest(link=LINK, twin_name="Twin")


async def test_a_missing_name_field_reports_control_categories_without_raw_labels() -> None:
    page = meet_page([], counts={NAME_SELECTOR: [0]}, labels=LABELS)
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert result.status == "failed"
    assert _leaked(result.detail) == []
    assert "visible controls: leave call, microphone toggle, participant tile action" in (
        result.detail
    )
    assert "unrecognised (2)" in result.detail


async def test_a_platform_block_reports_control_categories_without_raw_labels() -> None:
    page = meet_page([DENIED], evaluates=[NO_CAPTCHA], labels=LABELS)
    clock = FakeClock()

    result = await request_guest_join(
        page, _request(), polling=JoinPolling(clock.sleep, clock.monotonic)
    )

    assert result.status == "blocked"
    assert _leaked(result.detail) == []
    assert "visible controls: leave call, microphone toggle" in result.detail


async def test_a_failed_mic_toggle_reports_control_categories_without_raw_labels() -> None:
    page = meet_page(
        [], visible={MIC_ON_SELECTOR: False}, click_failures={MIC_OFF_SELECTOR}, labels=LABELS
    )
    clock = FakeClock()

    with pytest.raises(RuntimeError) as raised:
        await enable_meeting_microphone(page, clock.sleep)

    assert _leaked(str(raised.value)) == []
    assert "visible controls: leave call, microphone toggle" in str(raised.value)


class LeakingStatePage(FakePage):
    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        del kwargs
        return LeakingLocator(self, selector)


class LeakingLocator(FakeLocator):
    @override
    async def is_visible(self) -> bool:
        if self.selector == MIC_ON_SELECTOR:
            raise PlaywrightError(LEAK)
        return True

    @override
    async def wait_for(self, **kwargs: Any) -> None:
        del kwargs
        if self.selector == MIC_ON_SELECTOR:
            raise PlaywrightError(LEAK)


async def test_a_mic_state_check_error_never_carries_browser_error_text() -> None:
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="checking microphone state failed") as raised:
        await enable_meeting_microphone(LeakingStatePage([], []), clock.sleep)

    assert _leaked(str(raised.value)) == []


class LeakingConfirmationLocator(FakeLocator):
    @override
    async def is_visible(self) -> bool:
        return self.selector != MIC_ON_SELECTOR

    @override
    async def wait_for(self, **kwargs: Any) -> None:
        del kwargs
        if self.selector == MIC_ON_SELECTOR:
            raise PlaywrightError(LEAK)


class LeakingConfirmationPage(FakePage):
    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        del kwargs
        return LeakingConfirmationLocator(self, selector)


async def test_a_mic_confirmation_error_never_carries_browser_error_text() -> None:
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="waiting for enabled microphone failed") as raised:
        await enable_meeting_microphone(LeakingConfirmationPage([], []), clock.sleep)

    assert _leaked(str(raised.value)) == []


async def test_a_missing_leave_button_reports_control_categories_without_raw_labels() -> None:
    page = meet_page(["blank page without markers"], counts={LEAVE_SELECTOR: [0]}, labels=LABELS)
    clock = FakeClock()

    result = await leave_meeting(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.status == "failed"
    assert "leave button not found" in result.detail
    assert _leaked(result.detail) == []
    assert "visible controls: leave call, microphone toggle" in result.detail


async def test_a_session_join_failure_never_journals_or_returns_raw_labels() -> None:
    browser = FakePageSession(
        meet_page([], final_url=f"{LINK}?hl=en", counts={NAME_SELECTOR: [0]}, labels=LABELS)
    )
    journal = RecordingJournal()
    clock = FakeClock()

    result = await run_meeting_session(
        session_config(),
        SessionWiring(
            open_page=browser.open, sleep=clock.sleep, monotonic=clock.monotonic, journal=journal
        ),
    )

    assert (result.phase, result.terminal_reason) == ("ended", "browser_failure")
    assert _leaked(*journal.lines, result.detail) == []
    finished = journal.events("join_finished")
    assert "visible controls: leave call, microphone toggle" in finished[-1]["detail"]


@pytest.mark.parametrize(
    ("label", "category"),
    [
        ("Leave call", "leave call"),
        ("Turn on microphone", "microphone toggle"),
        ("Turn off your microphone", "microphone toggle"),
        ("Unmute microphone", "microphone toggle"),
        ("Turn off camera", "camera toggle"),
        ("Ask to join", "ask to join"),
        ("Join now", "join now"),
        ("Return to home screen", "return to home screen"),
        ("Submit feedback", "submit feedback"),
        ("Got it", "notice dismiss"),
        ("Sign in", "sign-in or sign-up"),
        ("Pin Rani Wijaya's tile", "participant tile action"),
        ("More options for rani.wijaya@example.com", "participant tile action"),
        ("Mute Rani Wijaya's microphone", "participant tile action"),
        ("More options", "call toolbar"),
        ("Chat with everyone", "call toolbar"),
        ("xqz-mnop-rst", "unrecognised"),
        ("Copy passcode 48213-SECRET-7731", "unrecognised"),
        ("Rani Wijaya (Presentation)", "unrecognised"),
        ("", "unrecognised"),
    ],
)
def test_control_labels_map_to_fixed_categories(label: str, category: str) -> None:
    assert control_category(label) == category


async def test_unknown_labels_are_counted_never_written() -> None:
    labels = ["Leave call", *[f"Rani Wijaya {index}" for index in range(60)]]
    page = meet_page([], labels={"button[aria-label]": labels})

    summary = await visible_control_categories(page)

    assert summary == f"leave call, unrecognised ({CONTROL_LABEL_LIMIT - 1})"


async def test_a_page_without_labels_reports_none_and_a_failed_read_reports_unknown() -> None:
    assert await visible_control_categories(meet_page([])) == "none"
    assert await visible_control_categories(BrokenLabelsPage([], [])) == "unknown"


class BrokenLabelsLocator(FakeLocator):
    @override
    async def evaluate_all(self, expression: str) -> list[Any]:
        del expression
        raise PlaywrightError(LEAK)


class BrokenLabelsPage(FakePage):
    @override
    def locator(self, selector: str, **kwargs: Any) -> Any:
        del kwargs
        return BrokenLabelsLocator(self, selector)


@pytest.mark.browser
async def test_a_real_page_without_a_leave_button_reports_only_control_categories() -> None:
    buttons = "".join(
        f'<button aria-label="{label}"></button>' for label in LABELS["button[aria-label]"][1:]
    )
    html = f"<main>{buttons}</main>"
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.route(
            FIXTURE_URL, lambda route: route.fulfill(body=html, content_type="text/html")
        )
        await page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        clock = FakeClock()

        result = await leave_meeting(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.status == "failed"
    assert _leaked(result.detail) == []
    assert result.detail.endswith(
        "visible controls: microphone toggle, participant tile action, unrecognised (2)"
    )
