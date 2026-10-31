import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cloakbrowser.human.actionability import ElementNotReceivingEventsError
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.meet.meet import CallObservation, CallState
from fakeyou.meet.watchdog import UIWatchdog, dismiss_notices, read_participant_count

IN_CALL = CallObservation("in_call", "call controls are visible")
LOBBY = CallObservation("lobby", "lobby controls are visible")


class _ImmediateTimeout:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def __aenter__(self) -> None:
        raise TimeoutError()

    async def __aexit__(self, *_args: Any) -> bool:
        return False


@dataclass
class FakeDialog:
    text: str
    roles: list[str]
    closes_on_escape: bool = False
    closes_on_click: bool = True
    visible: bool = True
    click_error: Exception | None = None
    open: bool = True
    pressed: list[str] = field(default_factory=list)
    clicks: int = 0

    def snapshot(self) -> str:
        return "\n".join(f'- {role} "{self.text[:20]}"' for role in ["dialog", *self.roles])


class FakeButton:
    def __init__(self, dialog: FakeDialog) -> None:
        self._dialog = dialog

    async def count(self) -> int:
        return self._dialog.roles.count("button")

    def nth(self, _position: int) -> "FakeButton":
        return self

    async def is_visible(self) -> bool:
        return True

    async def click(self, **_kwargs: Any) -> None:
        self._dialog.clicks += 1
        if self._dialog.click_error is not None:
            raise self._dialog.click_error
        if self._dialog.closes_on_click:
            self._dialog.open = False


class FakeDialogHandle:
    def __init__(self, dialog: FakeDialog) -> None:
        self._dialog = dialog

    async def is_visible(self) -> bool:
        return self._dialog.visible

    async def evaluate(self, _expression: str, **_kwargs: Any) -> str:
        return self._dialog.text

    async def aria_snapshot(self, **_kwargs: Any) -> str:
        return self._dialog.snapshot()

    async def press(self, key: str, **_kwargs: Any) -> None:
        self._dialog.pressed.append(key)
        if key == "Escape" and self._dialog.closes_on_escape:
            self._dialog.open = False


class FakeDialogs:
    def __init__(self, page: "FakeDialogPage") -> None:
        self._page = page

    async def count(self) -> int:
        return len(self._page.open_dialogs())

    def nth(self, index: int) -> FakeDialogHandle:
        return FakeDialogHandle(self._page.open_dialogs()[index])


class FakeDialogPage:
    def __init__(self, *dialogs: FakeDialog) -> None:
        self.dialogs = list(dialogs)

    def open_dialogs(self) -> list[FakeDialog]:
        return [dialog for dialog in self.dialogs if dialog.open]

    def locator(self, selector: str) -> Any:
        if selector.startswith("xpath="):
            found = re.search(r"\)\[(\d+)\]", selector)
            assert found is not None
            index = int(found.group(1)) - 1
            return FakeButton(self.open_dialogs()[index])
        return FakeDialogs(self)

    def get_by_role(self, _role: str) -> Any:
        labels = MagicMock()
        labels.evaluate_all = AsyncMock(return_value=[])
        return labels


def notice(text: str = "This meeting is being recorded Got it", **fields: Any) -> FakeDialog:
    return FakeDialog(text, ["button"], **fields)


def form(text: str = "Enter your name Continue") -> FakeDialog:
    return FakeDialog(text, ["textbox", "button"], closes_on_click=False)


def picker() -> FakeDialog:
    return FakeDialog("Skin tone", ["button"] * 6, closes_on_escape=True)


def watchdog_for(
    page: FakeDialogPage, *observations: CallObservation, now: list[float] | None = None
) -> UIWatchdog:
    clock = now if now is not None else [10.0]
    observe = AsyncMock(side_effect=[*observations] if observations else None)
    if not observations:
        observe.return_value = IN_CALL
    return UIWatchdog(cast(Page, page), observe=observe, monotonic=lambda: clock[0])


async def kinds_after_two_checks(watchdog: UIWatchdog) -> list[str]:
    first = await watchdog.check()
    second = await watchdog.check()
    return [event.kind for event in (*first.events, *second.events)]


@pytest.fixture
def page() -> MagicMock:
    result = MagicMock(spec=Page)
    result.locator.return_value.count = AsyncMock(return_value=0)
    result.get_by_role.return_value.evaluate_all = AsyncMock(return_value=[])
    return result


async def test_a_notice_that_closes_with_escape_is_dismissed_without_a_click() -> None:
    dialog = notice(closes_on_escape=True)

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_dismissed"]
    assert (dialog.pressed, dialog.clicks) == (["Escape"], 0)


async def test_a_notice_that_ignores_escape_is_closed_with_its_one_button() -> None:
    dialog = notice()

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_dismissed"]
    assert (dialog.pressed, dialog.clicks) == (["Escape"], 1)


async def test_a_choice_dialog_is_never_clicked_and_never_escalated() -> None:
    dialog = FakeDialog("Sign in to Google Sign in Dismiss", ["button", "button"])

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_observed"]
    assert dialog.clicks == 0


async def test_the_skin_tone_picker_closes_with_escape() -> None:
    dialog = picker()

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_dismissed"]
    assert dialog.clicks == 0


async def test_a_form_during_a_healthy_call_is_only_observed() -> None:
    dialog = form()

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_observed"]
    assert (dialog.pressed, dialog.clicks) == ([], 0)


async def test_a_form_escalates_only_once_the_call_worsens_and_then_expires() -> None:
    dialog = form()
    now = [10.0]
    watchdog = watchdog_for(
        FakeDialogPage(dialog), IN_CALL, IN_CALL, LOBBY, LOBBY, LOBBY, LOBBY, now=now
    )

    assert await kinds_after_two_checks(watchdog) == ["modal_observed"]
    await watchdog.check()
    escalated = await watchdog.check()
    now[0] = 400.0
    expired = await watchdog.check()

    assert [event.kind for event in escalated.events] == ["manual_action_required"]
    assert escalated.events[0].ttl_seconds == 180
    assert [event.kind for event in expired.events] == ["manual_action_expired"]
    assert dialog.clicks == 0


async def test_an_escalated_form_that_goes_away_is_cleared() -> None:
    dialog = form()
    watchdog = watchdog_for(FakeDialogPage(dialog), LOBBY, LOBBY, LOBBY)
    await watchdog.check()
    escalated = await watchdog.check()
    dialog.open = False

    cleared = await watchdog.check()

    assert [event.kind for event in escalated.events] == ["manual_action_required"]
    assert [event.kind for event in cleared.events] == ["manual_action_cleared"]


async def test_a_dialog_must_be_stable_for_two_checks_before_it_is_handled() -> None:
    dialog = notice()
    watchdog = watchdog_for(FakeDialogPage(dialog))

    first = await watchdog.check()
    dialog.roles = ["button", "link"]
    second = await watchdog.check()
    third = await watchdog.check()

    assert (first.events, second.events) == ((), ())
    assert [event.kind for event in third.events] == ["modal_dismissed"]


@pytest.mark.parametrize(
    "error", [PlaywrightError("detached"), ElementNotReceivingEventsError("covered")]
)
async def test_a_failed_close_is_observed_not_escalated(error: Exception) -> None:
    dialog = notice(click_error=error)

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == ["modal_observed"]


async def test_hidden_dialogs_are_ignored() -> None:
    dialog = notice(visible=False)

    assert await kinds_after_two_checks(watchdog_for(FakeDialogPage(dialog))) == []


async def test_activation_dismisses_only_notices() -> None:
    camera = FakeDialog("close Close error Camera not found", ["button"])
    choice = FakeDialog("Sign in Sign in Dismiss", ["button", "button"])
    page = FakeDialogPage(camera, choice, form())

    dismissed = await dismiss_notices(cast(Page, page))

    assert dismissed == ("close Close error Camera not found",)
    assert (camera.open, choice.open, choice.clicks) == (False, True, 0)


async def test_a_dismiss_bug_propagates_instead_of_failing_the_watchdog() -> None:
    watchdog = watchdog_for(FakeDialogPage(notice(click_error=ValueError("bug"))))
    await watchdog.check()

    with pytest.raises(ValueError, match="bug"):
        await watchdog.check()


async def test_should_back_off_after_three_quiet_checks_and_reset_on_modal() -> None:
    page = FakeDialogPage()
    watchdog = watchdog_for(page)
    assert (await watchdog.check()).next_check_seconds == 5
    assert (await watchdog.check()).next_check_seconds == 5
    assert (await watchdog.check()).next_check_seconds == 20
    page.dialogs.append(form())
    assert (await watchdog.check()).next_check_seconds == 5


async def test_should_require_two_identical_consecutive_lobby_observations() -> None:
    watchdog = watchdog_for(
        FakeDialogPage(),
        CallObservation("lobby", "waiting"),
        CallObservation("lobby", "join controls"),
        CallObservation("lobby", "join controls"),
        CallObservation("in_call", "controls"),
        CallObservation("lobby", "join controls"),
    )
    states = [(await watchdog.check()).observation.state for _ in range(5)]
    assert states == ["unknown", "unknown", "lobby", "in_call", "unknown"]


async def test_should_not_claim_recovery_when_observation_fails() -> None:
    observe = AsyncMock(side_effect=[IN_CALL, PlaywrightError("disconnected")])
    watchdog = UIWatchdog(cast(Page, FakeDialogPage(form())), observe=observe)
    await watchdog.check()
    result = await watchdog.check()
    assert [event.kind for event in result.events] == ["watchdog_failed"]
    assert result.observation.state == "unknown"
    assert result.next_check_seconds == 5


async def test_should_count_a_check_timeout_as_an_observation_failure(
    page: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    watchdog = UIWatchdog(cast(Page, page), observe=AsyncMock(return_value=IN_CALL))
    monkeypatch.setattr(asyncio, "timeout", _ImmediateTimeout)

    result = await watchdog.check()

    assert result.observation_failed is True
    assert [event.kind for event in result.events] == ["watchdog_failed"]


@pytest.mark.parametrize("state", ["in_call", "removed", "ended"])
async def test_should_preserve_call_observation_when_modal_scan_fails(
    page: MagicMock, state: CallState
) -> None:
    observe = AsyncMock(return_value=CallObservation(state, "definitive observation"))
    page.locator.side_effect = PlaywrightError("https://secret.example/token")
    result = await UIWatchdog(cast(Page, page), observe=observe).check()
    assert result.observation.state == state
    assert result.events[0].detail == "Error"


async def test_observe_bug_propagates_instead_of_failing_watchdog() -> None:
    observe = AsyncMock(side_effect=[IN_CALL, ValueError("bug")])
    watchdog = UIWatchdog(cast(Page, FakeDialogPage()), observe=observe)
    await watchdog.check()

    with pytest.raises(ValueError, match="bug"):
        await watchdog.check()


async def test_modal_scan_bug_propagates_instead_of_failing_watchdog(page: MagicMock) -> None:
    watchdog = UIWatchdog(cast(Page, page), observe=AsyncMock(return_value=IN_CALL))
    await watchdog.check()
    page.locator.side_effect = ValueError("bug")

    with pytest.raises(ValueError, match="bug"):
        await watchdog.check()


async def test_non_string_dialog_text_fails_watchdog_not_check() -> None:
    dialog = notice()
    dialog.text = cast(str, 12345)
    watchdog = watchdog_for(FakeDialogPage(dialog))

    result = await watchdog.check()

    assert [event.kind for event in result.events] == ["watchdog_failed"]


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


async def test_participant_count_bug_propagates(page: MagicMock) -> None:
    page.get_by_role.return_value.evaluate_all = AsyncMock(side_effect=ValueError("bug"))

    with pytest.raises(ValueError, match="bug"):
        await read_participant_count(cast(Page, page))


GATE_PAGE = """<meta charset="utf-8"><button aria-label="Leave call">Leave</button>
<button aria-label="People - 3 participants">People</button>
<div role="dialog" id="tone" aria-label="Skin tone">Skin tone
  <button>👍</button><button>👍🏻</button><button>👍🏼</button><button>👍🏽</button>
</div>
<div role="dialog" id="signin">Sign in to save your settings
  <button>Sign in</button><button>Not now</button>
</div>
<div role="dialog" id="countdown">Your call will end in 5 minutes. Upgrade for longer calls.
  <button onclick="this.parentElement.remove()">Got it</button>
</div>
<script>
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    for (const id of ['tone', 'signin']) {
      const node = document.getElementById(id);
      if (node && node.contains(document.activeElement)) node.remove();
    }
  }
});
for (const dialog of document.querySelectorAll('[role=dialog]')) dialog.tabIndex = -1;
</script>"""


@pytest.mark.browser
async def test_the_picker_the_sign_in_notice_and_the_countdown_never_end_the_session() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.route(
            "https://watchdog.fakeyou.test/gate",
            lambda route: route.fulfill(body=GATE_PAGE, content_type="text/html"),
        )
        await real_page.goto("https://watchdog.fakeyou.test/gate", wait_until="domcontentloaded")
        watchdog = UIWatchdog(real_page)
        first = await watchdog.check()
        second = await watchdog.check()
        third = await watchdog.check()
        remaining = await real_page.get_by_role("dialog").count()

    kinds = [event.kind for check in (first, second, third) for event in check.events]
    assert first.participant_count == 3
    assert "manual_action_required" not in kinds
    assert kinds.count("modal_dismissed") == 3
    assert remaining == 0


async def test_a_ticking_countdown_keeps_its_fingerprint() -> None:
    dialog = FakeDialog("Your call ends in 5 minutes", ["button", "button"])
    watchdog = watchdog_for(FakeDialogPage(dialog))

    await watchdog.check()
    handled = await watchdog.check()
    dialog.text = "Your call ends in 4 minutes"
    later = await watchdog.check()

    assert [event.kind for event in handled.events] == ["modal_observed"]
    assert later.events == ()
    assert dialog.pressed == ["Escape"]


async def test_typing_into_an_escalated_form_keeps_its_deadline() -> None:
    dialog = form()
    now = [10.0]
    watchdog = watchdog_for(FakeDialogPage(dialog), *[LOBBY] * 4, now=now)
    await watchdog.check()
    escalated = await watchdog.check()
    dialog.text = "Enter your name Rani Continue"
    now[0] = 400.0
    expired = await watchdog.check()

    assert [event.kind for event in escalated.events] == ["manual_action_required"]
    assert [event.kind for event in expired.events] == ["manual_action_expired"]
