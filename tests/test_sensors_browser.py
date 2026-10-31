import asyncio
import base64
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from fakeyou.config.settings import PerceptionSettings
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.perception.sensors import PerceptionDrain, PerceptionSensors, load_perception_script
from fakeyou.perception.timeline import MeetingTimeline, TimelineSnapshot
from tests._fakes_common import STREAM_CAPTURE_TEST_FLAG
from tests._meet_protocol_fakes import chat_message, collection_payload, output, user_info

AYU = "spaces/s/devices/ayu"
SCREEN = "spaces/s/devices/ayu-screen"
TEST_SCREEN_SHARE = """async () => {
  const canvas = document.createElement("canvas");
  canvas.width = 1280;
  canvas.height = 720;
  document.body.appendChild(canvas);
  const context = canvas.getContext("2d");
  let slide = ["", []];
  let tick = 0;
  const paint = () => {
    tick += 1;
    context.fillStyle = "#122048";
    context.fillRect(0, 0, 1280, 720);
    context.fillStyle = "#ffffff";
    context.font = "64px sans-serif";
    context.fillText(slide[0], 80, 140);
    context.fillStyle = "#ffc440";
    context.font = "40px sans-serif";
    slide[1].forEach((line, index) => context.fillText(line, 100, 260 + index * 80));
    context.fillStyle = tick % 2 ? "#122049" : "#122048";
    context.fillRect(1279, 719, 1, 1);
  };
  window.__fakeyouShow = (title, lines) => { slide = [title, lines]; paint(); };
  setInterval(paint, 100);
  const stream = canvas.captureStream(10);
  window.__fakeyouRtc.emitTestTrack(stream.getVideoTracks()[0], [stream]);
  const channel = new EventTarget();
  channel.label = "collections";
  window.__fakeyouRtc.emitTestChannel(channel);
  window.__fakeyouSend = (b64) => {
    const raw = atob(b64);
    const bytes = new Uint8Array(raw.length);
    for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
    channel.dispatchEvent(new MessageEvent("message", { data: bytes.buffer }));
  };
  window.__fakeyouKeep = { stream, channel };
  return stream.id;
}"""
SHOW_JS = "([title, lines]) => window.__fakeyouShow(title, lines)"
SEND_JS = "(b64) => window.__fakeyouSend(b64)"


async def _drain_until(
    drain: PerceptionDrain,
    timeline: MeetingTimeline,
    done: Callable[[TimelineSnapshot], bool],
    *,
    seconds: float = 15.0,
) -> TimelineSnapshot:
    async with asyncio.timeout(seconds):
        while True:
            await drain.drain_once()
            snapshot = timeline.snapshot()
            if done(snapshot):
                return snapshot
            await asyncio.sleep(0.25)


@pytest.mark.browser
async def test_the_eye_sees_a_shared_screen_through_the_meet_protocol() -> None:
    timeline = MeetingTimeline()
    sensors = PerceptionSensors(
        timeline, PerceptionSettings(enabled=True), joined_at=datetime.now(UTC)
    )
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(load_perception_script())
        await page.goto("about:blank")
        stream_id = await page.evaluate(TEST_SCREEN_SHARE)
        await page.evaluate(SHOW_JS, ["Launch Timeline", ["Alpha: 12 January 2027"]])
        drain = PerceptionDrain(page, sensors)
        sent_us = int(datetime.now(UTC).timestamp() * 1_000_000)
        payload = collection_payload(
            users=[user_info(AYU, "Ayu"), user_info(SCREEN, "", parent=AYU)],
            outputs=[output(SCREEN, stream_id)],
            chat=[chat_message("m1", AYU, "slides are up", sent_us)],
        )
        await page.evaluate(SEND_JS, base64.b64encode(payload).decode())

        first = await _drain_until(drain, timeline, lambda snap: snap.current_screen is not None)
        await page.evaluate(SHOW_JS, ["Pricing Plans", ["Pro: 299,000 per month"]])
        second = await _drain_until(drain, timeline, lambda snap: len(snap.screens) == 2)
        await page.evaluate(SHOW_JS, ["Launch Timeline", ["Alpha: 12 January 2027"]])
        back = await _drain_until(drain, timeline, lambda snap: snap.current_ref == "screen#1")

    assert first.presenting is not None
    assert first.presenting.name == "Ayu"
    assert [line.text for line in first.chat] == ["slides are up"]
    current = first.current_screen
    assert current is not None
    assert (current.keyframe.width, current.keyframe.height) == (1280, 720)
    assert second.current_ref == "screen#2"
    assert len(back.screens) == 2
    assert back.current_screen is not None
    assert len(back.current_screen.visits) == 2


@pytest.mark.browser
async def test_the_drain_reports_real_peer_connection_states() -> None:
    sensors = PerceptionSensors(
        MeetingTimeline(), PerceptionSettings(enabled=True), joined_at=datetime.now(UTC)
    )
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(load_perception_script())
        await page.goto("about:blank")
        await page.evaluate("() => { window.probePeer = new RTCPeerConnection(); }")
        drain = PerceptionDrain(page, sensors)
        await drain.drain_once()
        opened = sensors.peer_states
        await page.evaluate("() => window.probePeer.close()")
        await drain.drain_once()

    assert opened == ("new",)
    assert sensors.peer_states == ("closed",)
