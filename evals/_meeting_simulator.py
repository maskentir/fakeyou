import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from playwright.async_api import Page, Route

from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.meet.browser import open_direct_page
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from fakeyou.perception.meet_protocol import SYNC_COLLECTIONS_PATH
from tests._fakes_common import STREAM_CAPTURE_TEST_FLAG
from tests._meet_protocol_fakes import chat_message, collection_payload, sync_body, user_info

FIXTURES = Path(__file__).parent / "fixtures" / "simulator"
TWIN_DEVICE = "spaces/simulated/devices/twin"
LOBBY = 3
IN_CALL = 1
PLAY_LIMIT_SECONDS = 60.0
SHUTDOWN_LIMIT_SECONDS = 30.0

FAKE_MEET_PAGE = """<!doctype html><html><head><title>Meet</title></head><body>
<div id="app">
  <input placeholder="Your name" aria-label="Your name">
  <button id="ask">Ask to join</button>
</div>
<script>
const SYNC_URL = "https://meet.google.com/$rpc/google.rtc.meetings.v1.MeetingSpaceService/"
  + "SyncMeetingSpaceCollections";
const sync = () => fetch(SYNC_URL, { method: "POST", body: "sync" }).catch(() => {});
document.querySelector("#ask").onclick = () => {
  document.querySelector("#app").innerHTML = "<p>Asking to be let in…</p>";
  sync();
  window.__simSyncing = setInterval(sync, 1000);
};
const button = (label, text) => {
  const element = document.createElement("button");
  element.setAttribute("aria-label", label);
  element.textContent = text;
  return element;
};
window.__simLeft = false;
window.__simAdmit = () => {
  const app = document.querySelector("#app");
  app.innerHTML = "";
  app.append(button("Turn off microphone (⌘+d)", "Microphone"));
  const leave = button("Leave call", "Leave call");
  leave.onclick = () => {
    clearInterval(window.__simSyncing);
    window.__simLeft = true;
    fetch("https://meet.google.com/__sim/left", { method: "POST" }).catch(() => {});
    app.innerHTML = "<h1>You left the meeting</h1>";
  };
  app.append(leave);
  const hand = button("Raise hand (ctrl + alt + h)", "Hand");
  hand.id = "hand";
  hand.onclick = () => {
    const raised = hand.getAttribute("aria-label").startsWith("Lower");
    hand.setAttribute("aria-label", (raised ? "Raise hand" : "Lower hand") + " (ctrl + alt + h)");
  };
  app.append(hand);
  const panel = document.createElement("div");
  panel.style.display = "none";
  const chat = button("Chat with everyone", "Chat");
  chat.onclick = () => { panel.style.display = "block"; };
  app.append(chat);
  const box = document.createElement("textarea");
  box.setAttribute("maxlength", "4000");
  box.setAttribute("aria-label", "Send a message");
  const messages = document.createElement("div");
  messages.id = "messages";
  box.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const item = document.createElement("div");
    item.className = "sent";
    item.textContent = box.value;
    item.append(button("Pin message", "Pin"));
    messages.append(item);
    box.value = "";
  });
  panel.append(box, messages);
  app.append(panel);
  const channel = new EventTarget();
  channel.label = "collections";
  window.__fakeyouRtc.emitTestChannel(channel);
  window.__simKeep = { channel };
  window.__simSend = (b64) => {
    const raw = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
    channel.dispatchEvent(new MessageEvent("message", { data: bytes.buffer }));
  };
};
window.__simState = () => ({
  left: window.__simLeft,
  hand: document.querySelector("#hand")?.getAttribute("aria-label") ?? null,
  sent: [...document.querySelectorAll("#messages .sent")]
    .map((item) => item.firstChild.textContent),
});
window.__simVoices = {};
window.__simAudio = null;
const voice = (deviceId) => {
  window.__simAudio = window.__simAudio || new AudioContext();
  if (!window.__simVoices[deviceId]) {
    const destination = window.__simAudio.createMediaStreamDestination();
    const track = destination.stream.getAudioTracks()[0];
    const csrc = Object.keys(window.__simVoices).length + 1001;
    const receiver = {
      track,
      getContributingSources: () => [],
      getSynchronizationSources: () => [
        { source: csrc, audioLevel: 0.5, timestamp: performance.now() },
      ],
    };
    window.__fakeyouRtc.emitTestTrack(track, [destination.stream], receiver);
    const playback = window.__simAudio.createMediaStreamSource(destination.stream);
    const silent = window.__simAudio.createGain();
    silent.gain.value = 0;
    playback.connect(silent);
    silent.connect(window.__simAudio.destination);
    window.__simVoices[deviceId] = { destination, playback, silent };
  }
  return window.__simVoices[deviceId];
};
window.__simVoice = (deviceId) => voice(deviceId).destination.stream.id;
window.__simSay = async (deviceId, wav) => {
  const { destination } = voice(deviceId);
  const context = window.__simAudio;
  await context.resume();
  const raw = atob(wav);
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  const buffer = await context.decodeAudioData(bytes.buffer.slice(0));
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(destination);
  const audible = new Audio("data:audio/wav;base64," + wav);
  const heard = new Promise((resolve, reject) => {
    audible.onended = resolve;
    audible.onerror = () => reject(new Error("participant audio did not play"));
  });
  source.start();
  await audible.play();
  await heard;
  return buffer.duration;
};
</script></body></html>"""


@dataclass(slots=True)
class Participant:
    device_id: str
    name: str
    status: int = IN_CALL
    current: bool = False

    def as_user(self) -> dict[str, Any]:
        return user_info(self.device_id, self.name, status=self.status, current=self.current)


@dataclass(slots=True)
class MeetingSimulator:
    twin_name: str
    people: list[Participant] = field(default_factory=list)
    page_ready: asyncio.Event = field(default_factory=asyncio.Event)
    _page: Page | None = None
    _chats: int = 0
    left: bool = False

    def __post_init__(self) -> None:
        self.people.insert(0, Participant(TWIN_DEVICE, self.twin_name, LOBBY, current=True))

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("the worker has not opened the meeting page yet")
        return self._page

    async def join(self, device_id: str, name: str) -> None:
        self.people.append(Participant(device_id, name))
        await self.page.evaluate("(device) => window.__simVoice(device)", device_id)

    async def _serve_page(self, route: Route) -> None:
        await route.fulfill(body=FAKE_MEET_PAGE, content_type="text/html")

    async def _serve_sync(self, route: Route) -> None:
        users = [person.as_user() for person in self.people]
        await route.fulfill(body=sync_body(users), content_type="application/x-protobuf")

    @asynccontextmanager
    async def open_page(self, **options: Any) -> AsyncIterator[Page]:
        async with open_direct_page(**options) as page:
            await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
            serve_sync = self._serve_sync
            serve_page = self._serve_page

            async def sync_route(route: Route) -> None:
                await serve_sync(route)

            async def page_route(route: Route) -> None:
                await serve_page(route)

            async def left_route(route: Route) -> None:
                self.left = True
                await route.fulfill(status=204)

            await page.route("https://meet.google.com/**", page_route)
            await page.route(f"**/*{SYNC_COLLECTIONS_PATH}*", sync_route)
            await page.route("**/__sim/left", left_route)
            self._page = page
            self.page_ready.set()
            yield page

    async def run_session(self, config: SessionConfig, wiring: SessionWiring) -> SessionResult:
        return await run_meeting_session(config, replace(wiring, open_page=self.open_page))

    async def wait_for_lobby(self, session: asyncio.Task[Any], within: float = 60.0) -> None:
        ready = asyncio.ensure_future(self.page_ready.wait())
        done, _pending = await asyncio.wait(
            {ready, session}, timeout=within, return_when=asyncio.FIRST_COMPLETED
        )
        if session in done:
            ready.cancel()
            raise RuntimeError(
                f"the session ended before opening the meeting: {session.result()!r}"
            )
        if ready not in done:
            ready.cancel()
            raise TimeoutError(f"the worker did not open the meeting page within {within} seconds")
        await self.page.wait_for_function(
            "() => document.body.innerText.includes('Asking to be let in')", timeout=within * 1000
        )

    async def admit(self) -> None:
        self.people[0].status = IN_CALL
        await self.page.evaluate("() => window.__simAdmit()")

    async def chat(self, device_id: str, text: str) -> None:
        self._chats += 1
        sent_us = int(time.time() * 1_000_000)
        payload = collection_payload(
            chat=[chat_message(f"sim-chat-{self._chats}", device_id, text, sent_us)]
        )
        await self.page.evaluate(
            "(b64) => window.__simSend(b64)", base64.b64encode(payload).decode()
        )

    async def state(self) -> dict[str, Any]:
        state: dict[str, Any] = await self.page.evaluate("() => window.__simState()")
        return state

    async def wait_for_state(
        self, condition: Callable[[Mapping[str, Any]], bool], within: float = 60.0
    ) -> dict[str, Any]:
        async with asyncio.timeout(within):
            while True:
                current = await self.state()
                if condition(current):
                    return current
                await asyncio.sleep(0.25)

    async def say(self, clip: str, *, speaker: str) -> float:
        wav = base64.b64encode((FIXTURES / clip).read_bytes()).decode()
        async with asyncio.timeout(PLAY_LIMIT_SECONDS):
            seconds: float = await self.page.evaluate(
                "([device, wav]) => window.__simSay(device, wav)", [speaker, wav]
            )
        return seconds


@asynccontextmanager
async def running_session(work: Coroutine[Any, Any, Any]) -> AsyncIterator[asyncio.Task[Any]]:
    session = asyncio.ensure_future(work)
    try:
        yield session
    finally:
        if not session.done():
            session.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                async with asyncio.timeout(SHUTDOWN_LIMIT_SECONDS):
                    await session


def journal_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def wait_for_event(
    path: Path,
    event: str,
    *,
    matching: Callable[[Mapping[str, Any]], bool] = lambda _row: True,
    within: float = 60.0,
) -> dict[str, Any]:
    async with asyncio.timeout(within):
        while True:
            for row in journal_rows(path):
                if row.get("event") == event and matching(row):
                    return row
            await asyncio.sleep(0.25)
