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
from tests._meet_protocol_fakes import (
    chat_message,
    collection_payload,
    output,
    sync_body,
    user_info,
)

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
window.__simReactions = [];
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
  const picker = document.createElement("div");
  picker.setAttribute("role", "dialog");
  picker.setAttribute("aria-label", "Send a reaction");
  picker.style.display = "none";
  for (const emoji of ["\\u{1F496}", "\\u{1F44D}", "\\u{1F389}", "\\u{1F44F}", "\\u{1F602}",
    "\\u{1F62E}", "\\u{1F622}", "\\u{1F914}", "\\u{1F44E}"]) {
    const choice = button(emoji, emoji);
    choice.onclick = () => window.__simReactions.push(emoji);
    picker.append(choice);
  }
  const reactions = button("Send a reaction", "Reactions");
  reactions.onclick = () => { picker.style.display = "block"; };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") picker.style.display = "none";
  });
  app.append(reactions, picker);
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
  reactions: window.__simReactions,
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
window.__simVideos = {};
const shape = (context, color, points) => {
  context.fillStyle = color;
  context.beginPath();
  points.forEach(([x, y], index) => (index ? context.lineTo(x, y) : context.moveTo(x, y)));
  context.closePath();
  context.fill();
};
const oval = (context, color, x, y, radiusX, radiusY, from = 0, to = Math.PI * 2) => {
  context.fillStyle = color;
  context.beginPath();
  context.ellipse(x, y, radiusX, radiusY, 0, from, to);
  context.fill();
};
const drawPerson = (context, shirt) => {
  const skin = "#e0ad86";
  const hair = "#2b1d14";
  context.fillStyle = "#bfb6a3";
  context.fillRect(0, 0, 1280, 90);
  context.fillStyle = "#8a6b4a";
  context.fillRect(980, 120, 200, 300);
  shape(context, shirt, [[380, 720], [400, 520], [470, 450], [560, 420], [720, 420],
    [810, 450], [880, 520], [900, 720]]);
  shape(context, shirt, [[400, 520], [300, 640], [340, 690], [440, 600]]);
  shape(context, shirt, [[880, 520], [980, 640], [940, 690], [840, 600]]);
  context.fillStyle = "#d9a07a";
  context.fillRect(600, 360, 80, 80);
  shape(context, "rgba(0, 0, 0, 0.18)", [[560, 420], [640, 500], [720, 420], [690, 420],
    [640, 470], [590, 420]]);
  oval(context, skin, 640, 270, 110, 130);
  oval(context, hair, 640, 220, 120, 110, Math.PI, Math.PI * 2);
  oval(context, hair, 597, 255, 12, 10);
  oval(context, hair, 682, 255, 12, 10);
  oval(context, skin, 533, 270, 15, 30);
  oval(context, skin, 747, 270, 15, 30);
  context.strokeStyle = "#7a3b2e";
  context.lineWidth = 6;
  context.beginPath();
  context.arc(640, 300, 40, Math.PI * 0.15, Math.PI * 0.85);
  context.stroke();
};
const paintScene = (context, scene, tick) => {
  const { width, height } = context.canvas;
  context.fillStyle = scene.background;
  context.fillRect(0, 0, width, height);
  if (scene.shirt) drawPerson(context, scene.shirt);
  context.fillStyle = "#ffffff";
  context.font = "64px sans-serif";
  context.fillText(scene.title ?? "", 80, 140);
  context.fillStyle = "#ffc440";
  context.font = "40px sans-serif";
  (scene.lines ?? []).forEach((line, index) => context.fillText(line, 100, 260 + index * 80));
  context.fillStyle = tick % 2 ? "#010101" : "#000000";
  context.fillRect(width - 1, height - 1, 1, 1);
};
window.__simPaint = (key, scene) => {
  if (!window.__simVideos[key]) {
    const canvas = document.createElement("canvas");
    canvas.width = 1280;
    canvas.height = 720;
    const context = canvas.getContext("2d");
    const entry = { context, scene, tick: 0 };
    entry.timer = setInterval(() => {
      entry.tick += 1;
      paintScene(context, entry.scene, entry.tick);
    }, 100);
    const stream = canvas.captureStream(10);
    entry.stream = stream;
    window.__fakeyouRtc.emitTestTrack(stream.getVideoTracks()[0], [stream]);
    window.__simVideos[key] = entry;
  }
  const entry = window.__simVideos[key];
  entry.scene = scene;
  paintScene(entry.context, scene, entry.tick);
  return entry.stream.id;
};
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
  const started = performance.timeOrigin + performance.now();
  source.start();
  await audible.play();
  await heard;
  return { start: started / 1000, end: (performance.timeOrigin + performance.now()) / 1000 };
};
window.__simTwin = { segments: [], chunks: [] };
window.__simListen = async () => {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const context = new AudioContext();
  await context.resume();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  let open = null;
  setInterval(() => {
    analyser.getFloatTimeDomainData(samples);
    const energy = samples.reduce((total, value) => total + value * value, 0);
    const now = (performance.timeOrigin + performance.now()) / 1000;
    if (Math.sqrt(energy / samples.length) > 0.01) {
      if (!open) {
        open = { start: now, end: now };
        window.__simTwin.segments.push(open);
      }
      open.end = now;
    } else if (open && now - open.end > 0.4) {
      open = null;
    }
  }, 20);
  const recorder = new MediaRecorder(stream, { mimeType: "audio/webm" });
  recorder.ondataavailable = (event) => window.__simTwin.chunks.push(event.data);
  recorder.start(1000);
  window.__simKeep.listening = { stream, context, recorder };
};
window.__simRecording = async () => {
  const blob = new Blob(window.__simTwin.chunks, { type: "audio/webm" });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let raw = "";
  for (let index = 0; index < bytes.length; index += 1) raw += String.fromCharCode(bytes[index]);
  return btoa(raw);
};
</script></body></html>"""


@dataclass(frozen=True, slots=True)
class Span:
    start: float
    end: float


@dataclass(slots=True)
class Participant:
    device_id: str
    name: str
    status: int = IN_CALL
    current: bool = False
    parent: str = ""

    def as_user(self) -> dict[str, Any]:
        return user_info(
            self.device_id, self.name, status=self.status, parent=self.parent, current=self.current
        )


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
        await self.page.evaluate("() => window.__simListen()")

    async def _send(self, payload: bytes) -> None:
        await self.page.evaluate(
            "(b64) => window.__simSend(b64)", base64.b64encode(payload).decode()
        )

    async def chat(self, device_id: str, text: str) -> None:
        self._chats += 1
        sent_us = int(time.time() * 1_000_000)
        await self._send(
            collection_payload(
                chat=[chat_message(f"sim-chat-{self._chats}", device_id, text, sent_us)]
            )
        )

    async def _show(self, device_id: str, scene: Mapping[str, Any]) -> None:
        stream_id: str = await self.page.evaluate(
            "([key, scene]) => window.__simPaint(key, scene)", [device_id, dict(scene)]
        )
        await self._send(
            collection_payload(
                users=[person.as_user() for person in self.people],
                outputs=[output(device_id, stream_id)],
            )
        )

    async def share(self, presenter: str, title: str, lines: list[str]) -> None:
        screen = f"{presenter}/screen"
        if all(person.device_id != screen for person in self.people):
            self.people.append(Participant(screen, "", parent=presenter))
        await self._show(screen, {"background": "#122048", "title": title, "lines": lines})

    async def camera(self, device_id: str, shirt: str) -> None:
        await self._show(device_id, {"background": "#d8d2c4", "shirt": shirt})

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

    async def say(self, clip: str, *, speaker: str) -> Span:
        wav = base64.b64encode((FIXTURES / clip).read_bytes()).decode()
        async with asyncio.timeout(PLAY_LIMIT_SECONDS):
            spoken = await self.page.evaluate(
                "([device, wav]) => window.__simSay(device, wav)", [speaker, wav]
            )
        return Span(**spoken)

    async def twin_voice(self) -> list[Span]:
        segments = await self.page.evaluate("() => window.__simTwin.segments")
        return [Span(**segment) for segment in segments]

    async def wait_for_twin_voice(
        self, condition: Callable[[Span], bool], within: float = 60.0
    ) -> Span:
        async with asyncio.timeout(within):
            while True:
                found = [span for span in await self.twin_voice() if condition(span)]
                if found:
                    return found[0]
                await asyncio.sleep(0.1)

    async def twin_recording(self) -> bytes:
        return base64.b64decode(await self.page.evaluate("() => window.__simRecording()"))


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
