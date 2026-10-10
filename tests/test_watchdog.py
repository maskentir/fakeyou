from typing import Any, cast
from unittest.mock import DEFAULT, AsyncMock, MagicMock

import pytest
from playwright.async_api import Page

from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import CallObservation
from fakeyou.meet.watchdog import UIWatchdog, read_participant_count


@pytest.fixture
def page() -> MagicMock:
    result = MagicMock(spec=Page)
    result.locator.return_value.count = AsyncMock(return_value=0)
    result.get_by_role.return_value.evaluate_all = AsyncMock(return_value=[])
    return result


@pytest.fixture
def observe() -> AsyncMock:
    return AsyncMock(return_value=CallObservation("in_call", "call controls are visible"))


def show_dialog(page, text, *, visible=True, button_count=1):
    dialog = MagicMock()
    dialog.is_visible = AsyncMock(return_value=visible)
    dialog.evaluate = AsyncMock(return_value=text)
    dialog.get_by_role.return_value.count = AsyncMock(return_value=button_count)
    button = MagicMock()
    button.first.click = AsyncMock()
    dialog.button = button.first
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.locator.return_value.nth.return_value = dialog
    page.locator.side_effect = lambda selector: button if selector.startswith("xpath=") else DEFAULT
    return dialog


async def test_should_back_off_after_three_quiet_checks_and_reset_on_modal(
    page: MagicMock, observe: AsyncMock
) -> None:
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    assert (await watchdog.check()).next_check_seconds == 5
    assert (await watchdog.check()).next_check_seconds == 5
    assert (await watchdog.check()).next_check_seconds == 20
    show_dialog(page, "Sign in to continue")
    result = await watchdog.check()
    assert result.next_check_seconds == 5
    assert result.events == ()
    result = await watchdog.check()
    assert result.events[0].kind == "manual_action_required"
    assert result.participant_count is None


async def test_should_require_two_identical_consecutive_lobby_observations(
    page: MagicMock, observe: AsyncMock
) -> None:
    observe.side_effect = [
        CallObservation("lobby", "waiting"),
        CallObservation("lobby", "join controls"),
        CallObservation("lobby", "join controls"),
        CallObservation("in_call", "controls"),
        CallObservation("lobby", "join controls"),
    ]
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    states = [(await watchdog.check()).observation.state for _ in range(5)]
    assert states == ["unknown", "unknown", "lobby", "in_call", "unknown"]


async def test_should_deduplicate_unknown_dialog_and_expire_once_without_clicking(
    page: MagicMock, observe: AsyncMock
) -> None:
    dialog = show_dialog(page, "Grant account access Continue")
    now = [10.0]
    watchdog = UIWatchdog(cast(Page, page), observe=observe, monotonic=lambda: now[0])
    assert (await watchdog.check()).events == ()
    first = await watchdog.check()
    assert first.events[0].kind == "manual_action_required"
    assert first.events[0].ttl_seconds == 180
    assert (await watchdog.check()).events == ()
    now[0] = 190.0
    assert (await watchdog.check()).events[0].kind == "manual_action_expired"
    assert (await watchdog.check()).events == ()
    dialog.button.click.assert_not_awaited()


async def test_should_clear_recovered_dialog_and_alert_when_it_returns(
    page: MagicMock, observe: AsyncMock
) -> None:
    show_dialog(page, "Sign in to continue")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    first = await watchdog.check()
    page.locator.return_value.count.return_value = 0
    recovered = await watchdog.check()
    assert recovered.events[0].kind == "manual_action_cleared"
    assert recovered.events[0].fingerprint == first.events[0].fingerprint
    page.locator.return_value.count.return_value = 1
    assert (await watchdog.check()).events == ()
    assert (await watchdog.check()).events[0].kind == "manual_action_required"


async def test_should_clear_replaced_dialog_and_report_new_modal(
    page: MagicMock, observe: AsyncMock
) -> None:
    dialog = show_dialog(page, "First unknown dialog")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    await watchdog.check()
    dialog.evaluate.return_value = "Second unknown dialog"
    result = await watchdog.check()
    assert [event.kind for event in result.events] == ["manual_action_cleared"]
    assert (await watchdog.check()).events[0].kind == "manual_action_required"


@pytest.mark.parametrize(
    ("text", "button_count", "expected"),
    [
        ("This meeting is being recorded Got it", 1, "modal_dismissed"),
        ("This meeting is being transcribed Got it", 1, "modal_dismissed"),
        ("This meeting is being recorded Got it", 2, "manual_action_required"),
        ("Allow recording Agree", 1, "manual_action_required"),
        ("This meeting is being recorded Got it Allow access", 1, "manual_action_required"),
        ("Unknown warning OK", 1, "manual_action_required"),
    ],
)
async def test_should_only_dismiss_explicit_harmless_notice(
    page: MagicMock, observe: AsyncMock, text: str, button_count: int, expected: str
) -> None:
    dialog = show_dialog(page, text, button_count=button_count)
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    result = await watchdog.check()
    assert result.events[-1].kind == expected
    if expected == "manual_action_required":
        dialog.button.click.assert_not_awaited()


async def test_should_escalate_when_allowlisted_dismissal_fails(
    page: MagicMock, observe: AsyncMock
) -> None:
    dialog = show_dialog(page, "This meeting is being recorded Got it")
    dialog.button.click.side_effect = RuntimeError("detached")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    result = await watchdog.check()
    assert [event.kind for event in result.events] == ["watchdog_failed", "manual_action_required"]


CAMERA_NOT_FOUND_DIALOG = "close Close error Camera not found Make sure your camera is plugged in"


@pytest.mark.parametrize("button_count", [1, 2, 3])
async def test_should_dismiss_camera_not_found_dialog(
    page: MagicMock, observe: AsyncMock, button_count: int
) -> None:
    dialog = show_dialog(page, CAMERA_NOT_FOUND_DIALOG, button_count=button_count)
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    result = await watchdog.check()
    assert result.events[-1].kind == "modal_dismissed"
    [selector] = [
        call.args[0] for call in page.locator.call_args_list if call.args[0].startswith("xpath=")
    ]
    assert '@aria-label="Close"' in selector
    dialog.button.click.assert_awaited_once()


async def test_should_escalate_when_camera_dismissal_fails(
    page: MagicMock, observe: AsyncMock
) -> None:
    dialog = show_dialog(page, CAMERA_NOT_FOUND_DIALOG)
    dialog.button.click.side_effect = RuntimeError("detached")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    result = await watchdog.check()
    assert [event.kind for event in result.events] == ["watchdog_failed", "manual_action_required"]


async def test_should_not_claim_recovery_when_observation_fails(
    page: MagicMock, observe: AsyncMock
) -> None:
    show_dialog(page, "Unknown dialog")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    await watchdog.check()
    observe.side_effect = RuntimeError("disconnected")
    result = await watchdog.check()
    assert [event.kind for event in result.events] == ["watchdog_failed"]
    assert result.observation.state == "unknown"
    assert result.next_check_seconds == 5


async def test_should_ignore_hidden_dialogs(page: MagicMock, observe: AsyncMock) -> None:
    show_dialog(page, "Unknown dialog", visible=False)
    assert (await UIWatchdog(cast(Page, page), observe=observe).check()).events == ()


@pytest.mark.parametrize("state", ["in_call", "removed", "ended"])
async def test_should_preserve_call_observation_when_modal_scan_fails(
    page: MagicMock, observe: AsyncMock, state: str
) -> None:
    observe.return_value = CallObservation(state, "definitive observation")
    page.locator.side_effect = RuntimeError("https://secret.example/token")
    result = await UIWatchdog(cast(Page, page), observe=observe).check()
    assert result.observation.state == state
    assert result.events[0].detail == "RuntimeError"


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["People - 3 participants"], 3),
        (["People - 1 participant"], 1),
        (["People - 0 participants"], None),
        (["People - 2 participants", "People - 3 participants"], None),
        (["Show everyone", "3"], None),
        (["People - 2 participants requesting admission"], None),
        ([], None),
    ],
)
async def test_should_only_count_explicit_unambiguous_people_labels(
    page: MagicMock, labels: list[Any], expected: int | None
) -> None:
    page.get_by_role.return_value.evaluate_all.return_value = labels
    assert await read_participant_count(cast(Page, page)) == expected


async def test_should_reset_modal_stability_when_content_changes(
    page: MagicMock, observe: AsyncMock
) -> None:
    dialog = show_dialog(page, "First unknown dialog")
    watchdog = UIWatchdog(cast(Page, page), observe=observe)
    assert (await watchdog.check()).events == ()
    dialog.evaluate.return_value = "Second unknown dialog"
    assert (await watchdog.check()).events == ()
    assert (await watchdog.check()).events[0].kind == "manual_action_required"


@pytest.mark.browser
async def test_should_read_visible_people_count_and_dismiss_only_safe_fixture_modal() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        notice = """<button aria-label="Leave call">Leave</button>
            <button aria-label="People - 3 participants">People</button>
            <button style="display:none" aria-label="People - 99 participants">Hidden</button>
            <div role="dialog">This meeting is being recorded
              <button onclick="this.parentElement.remove()">Got it</button>
            </div>"""
        await real_page.route(
            "https://watchdog.fakeyou.test/notice",
            lambda route: route.fulfill(body=notice, content_type="text/html"),
        )
        await real_page.goto("https://watchdog.fakeyou.test/notice", wait_until="domcontentloaded")
        watchdog = UIWatchdog(real_page)
        first = await watchdog.check()
        assert first.participant_count == 3
        assert first.events == ()
        second = await watchdog.check()
        assert second.events[0].kind == "modal_dismissed"
        assert await real_page.get_by_role("dialog").count() == 0
        unknown_notice = """<button aria-label="Leave call">Leave</button>
            <div role="dialog">Grant account access<button>OK</button></div>"""
        await real_page.route(
            "https://watchdog.fakeyou.test/unknown",
            lambda route: route.fulfill(body=unknown_notice, content_type="text/html"),
        )
        await real_page.goto("https://watchdog.fakeyou.test/unknown", wait_until="domcontentloaded")
        await watchdog.check()
        unknown = await watchdog.check()
        assert unknown.events[0].kind == "manual_action_required"
        assert await real_page.get_by_role("dialog").count() == 1
