import pytest

from fakeyou.browser import open_direct_page
from fakeyou.meet_actions import (
    MeetActionSignals,
    lower_hand,
    raise_hand,
    send_chat,
)
from tests.fakes import IN_CALL, LOBBY, FakeClock, FakePage, meet_page


def _button_xpath(prefix: str) -> str:
    return (
        "xpath=//button[starts-with(translate(@aria-label, "
        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{prefix}')]"
    )


RAISE_XPATH = _button_xpath("raise hand")
LOWER_XPATH = _button_xpath("lower hand")
CHAT_BUTTON_XPATH = _button_xpath("chat")
CHAT_INPUT_XPATH = "xpath=//textarea[@maxlength='4000']"
ECHO_ITEM_XPATH = _button_xpath("pin message")

CHAT_TEXT = "twin check-in ping"
ECHO_CONTENT = IN_CALL + f'<div>{CHAT_TEXT}<button aria-label="Pin message">Pin</button></div>'
TEXT_WITHOUT_PIN = IN_CALL + f"<div>{CHAT_TEXT}</div>"

FIXTURE_URL = "https://meet-actions.fakeyou.test/fixture"
FIXTURE_HTML = """<button aria-label="Leave call">Leave</button>
<button id="hand" aria-label="Raise hand (ctrl + alt + h)">Hand</button>
<button id="chatbtn" aria-label="Chat with everyone">Chat</button>
<div id="panel" style="display:none">
<textarea maxlength="4000" id="box" aria-label="Send a message"></textarea>
<div id="messages"></div>
</div>
<script>
const hand = document.getElementById('hand');
hand.addEventListener('click', () => {
  const raised = hand.getAttribute('aria-label').startsWith('Lower');
  const next = raised ? 'Raise hand' : 'Lower hand';
  hand.setAttribute('aria-label', next + ' (ctrl + alt + h)');
});
const panel = document.getElementById('panel');
document.getElementById('chatbtn').addEventListener('click', () => {
  panel.style.display = 'block';
});
const box = document.getElementById('box');
box.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') {
    event.preventDefault();
    const item = document.createElement('div');
    item.textContent = event.target.value;
    const pin = document.createElement('button');
    pin.setAttribute('aria-label', 'Pin message');
    pin.textContent = 'Pin';
    item.appendChild(pin);
    document.getElementById('messages').appendChild(item);
  }
});
</script>
"""


def _with_enter(page: FakePage) -> list[str]:
    pressed: list[str] = []

    async def press(key: str) -> None:
        pressed.append(key)

    page.keyboard.press = press
    return pressed


async def test_raise_hand_noop_when_already_raised() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: True})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert "already" in result.detail
    assert page.clicked == []


async def test_raise_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False})
    clock = FakeClock()

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH]


async def test_lower_hand_clicks_then_succeeds() -> None:
    page = meet_page([IN_CALL], visible={RAISE_XPATH: False})
    clock = FakeClock()

    result = await lower_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [LOWER_XPATH]


async def test_raise_hand_status_timeout_raises() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, wait_failures={LOWER_XPATH})
    clock = FakeClock()

    with pytest.raises(RuntimeError, match="raised"):
        await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == [RAISE_XPATH]


async def test_hand_click_retries_three_times_with_fresh_locator() -> None:
    page = meet_page([IN_CALL], visible={LOWER_XPATH: False}, click_flaky={RAISE_XPATH: 2})
    clock = FakeClock()
    locate_calls: list[str] = []
    real_locator = page.locator

    def counting(selector: str):
        locate_calls.append(selector)
        return real_locator(selector)

    page.locator = counting

    result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [RAISE_XPATH, RAISE_XPATH, RAISE_XPATH]
    assert locate_calls.count(RAISE_XPATH) == 3


async def test_send_chat_opens_panel_types_and_succeeds_on_echo() -> None:
    page = meet_page([IN_CALL, ECHO_CONTENT], visible={CHAT_INPUT_XPATH: False})
    clock = FakeClock()
    pressed = _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [CHAT_BUTTON_XPATH, CHAT_INPUT_XPATH]
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]
    assert CHAT_TEXT not in result.detail


async def test_send_chat_fails_without_echo_and_never_resends() -> None:
    page = meet_page([IN_CALL] + [IN_CALL] * 11)
    clock = FakeClock()
    pressed = _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert "not observed" in result.detail
    assert CHAT_TEXT not in result.detail
    assert page.typed == [CHAT_TEXT]
    assert pressed == ["Enter"]


async def test_send_chat_fails_when_pin_item_missing() -> None:
    page = meet_page([IN_CALL] + [TEXT_WITHOUT_PIN] * 11, counts={ECHO_ITEM_XPATH: [0] * 11})
    clock = FakeClock()
    _with_enter(page)

    result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.failed
    assert page.typed == [CHAT_TEXT]


@pytest.mark.parametrize("action", ["raise", "lower", "chat"])
async def test_actions_refuse_fast_when_not_admitted(action: str) -> None:
    page = meet_page([LOBBY])
    clock = FakeClock()
    _with_enter(page)

    if action == "raise":
        result = await raise_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)
    elif action == "lower":
        result = await lower_hand(page, sleep=clock.sleep, monotonic=clock.monotonic)
    else:
        result = await send_chat(page, CHAT_TEXT, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.refused
    assert "lobby controls are visible" in result.detail
    assert page.clicked == []
    assert page.typed == []
    assert clock.sleeps == []


async def test_send_chat_rejects_empty_text_without_touching_dom() -> None:
    page = meet_page([])
    clock = FakeClock()

    with pytest.raises(ValueError, match="empty"):
        await send_chat(page, "   ", sleep=clock.sleep, monotonic=clock.monotonic)

    assert page.clicked == []
    assert page.typed == []


async def test_custom_signals_drive_selectors() -> None:
    signals = MeetActionSignals(raise_hand="lever la main", lower_hand="baisser la main")
    page = meet_page([IN_CALL], visible={_button_xpath("baisser la main"): False})
    clock = FakeClock()

    result = await raise_hand(page, signals, sleep=clock.sleep, monotonic=clock.monotonic)

    assert result.succeeded
    assert page.clicked == [_button_xpath("lever la main")]


@pytest.mark.browser
async def test_hand_and_chat_actions_against_fixture_page() -> None:
    async with open_direct_page(headless=True) as real_page:
        await real_page.route(
            FIXTURE_URL,
            lambda route: route.fulfill(body=FIXTURE_HTML, content_type="text/html"),
        )
        await real_page.goto(FIXTURE_URL, wait_until="domcontentloaded")
        raised = await raise_hand(real_page)
        assert raised.succeeded
        lowered = await lower_hand(real_page)
        assert lowered.succeeded
        sent = await send_chat(real_page, "live fixture ping")
        assert sent.succeeded
