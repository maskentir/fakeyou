from tests._meet_browser_fakes import IN_CALL, FakePage


def button_xpath(prefix: str) -> str:
    return (
        "xpath=//button[starts-with(translate(@aria-label, "
        f"'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '{prefix}')]"
    )


RAISE_XPATH = button_xpath("raise hand")
LOWER_XPATH = button_xpath("lower hand")
CHAT_BUTTON_XPATH = button_xpath("chat")
CHAT_INPUT_XPATH = "xpath=//textarea[@maxlength='4000']"
CHAT_TEXT = "twin check-in ping"
ECHO_CONTENT = IN_CALL + f'<div>{CHAT_TEXT}<button aria-label="Pin message">Pin</button></div>'
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


def with_enter(page: FakePage) -> list[str]:
    pressed: list[str] = []

    async def press(key: str) -> None:
        pressed.append(key)

    page.keyboard.press = press
    return pressed
