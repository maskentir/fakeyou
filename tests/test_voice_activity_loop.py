import asyncio
from typing import Any

import pytest

from fakeyou.audio.deaf import CaptureActivity
from fakeyou.audio.graph import AudioGraph, GraphParts
from fakeyou.audio.voice_activity import (
    VOICE_ACTIVITY_DRAIN_JS,
    VoiceActivityLoop,
    VoiceActivityTracker,
    load_voice_activity_script,
)
from fakeyou.config.identifiers import SessionId
from fakeyou.conversation.session import SessionConfig
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from tests._audio_support import NullSTT, ScriptedSpeech
from tests._fakes_common import LINK, FakeClock
from tests._meet_browser_fakes import IN_CALL, FakePageSession, HangingPage, meet_page
from tests._persistence_fakes import FakeSaverSession, RecordingJournal
from tests._voice_support import batch, tracker_at


async def test_voice_activity_loop_feeds_tracker_and_survives_failures() -> None:
    from playwright.async_api import Error as PlaywrightError

    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    page = meet_page([], evaluates=[batch(10_000.0, []), None, PlaywrightError("gone")])
    loop = VoiceActivityLoop(page, tracker)
    assert await loop.poll() is True
    assert await loop.poll() is False
    assert await loop.poll() is False
    assert (loop.polls, loop.poll_failures, loop.last_poll_error) == (3, 2, "Error")
    assert page.evaluate_calls == [VOICE_ACTIVITY_DRAIN_JS] * 3


async def test_voice_activity_loop_bounds_a_hung_drain_evaluate() -> None:
    tracker = tracker_at(FakeClock(now=10.0))
    page = HangingPage()
    loop = VoiceActivityLoop(page, tracker, poll_interval_seconds=0.01)

    assert await loop.poll() is False

    assert page.cancelled is True
    assert (loop.poll_failures, loop.last_poll_error) == (1, "TimeoutError")


async def test_capture_observer_reports_vad_edges_to_voice_tracker() -> None:
    clock = FakeClock(now=10.0)
    tracker = tracker_at(clock)
    speech = ScriptedSpeech()
    graph = AudioGraph(
        GraphParts(
            activity=CaptureActivity(now=clock.monotonic),
            vad=speech.controller,
            stt=NullSTT(),
            voice_activity=tracker,
        )
    )
    await graph.ingest(speech.speaking(8))
    clock.now = 11.0
    await graph.ingest(speech.silent(8))
    tracker.observe_batch(
        batch(11_000.0, [[10_300.0, 3, 0.3]]), sent_ms=11_000.0, received_ms=11_000.0
    )
    assert tracker.attribute().slot == 3


async def test_run_meeting_session_installs_voice_script_and_starts_drain() -> None:
    stop = asyncio.Event()
    clock = FakeClock()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
        evaluates=[batch(0.0, [])] * 8,
    )
    tracker = VoiceActivityTracker(twin_name="Twin", monotonic=clock.monotonic)
    config = SessionConfig(
        owner_id=SessionId("o"),
        twin_id=SessionId("t"),
        meeting_id=SessionId("m"),
        link=LINK,
        twin_name="Twin",
    )
    journal = RecordingJournal("m")
    result = await run_meeting_session(
        config,
        SessionWiring(
            stop=stop,
            open_page=FakePageSession(page).open,
            saver_factory=FakeSaverSession().factory,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            journal=journal,
            voice_activity=tracker,
        ),
    )
    assert result.phase == "ended"
    assert page.init_scripts == [load_voice_activity_script()]
    assert journal.events("voice_activity_started")
    [stopped] = journal.events("voice_activity_stopped")
    assert stopped["polls"] >= 1
    assert stopped["poll_failures"] == 0
    assert stopped["last_poll_error"] is None


@pytest.mark.browser
async def test_voice_script_taps_remote_audio_and_skips_local_microphone() -> None:
    fake_microphone = (
        "navigator.mediaDevices.getUserMedia = async () => {"
        " const ctx = new AudioContext();"
        " const tone = ctx.createOscillator();"
        " const sink = ctx.createMediaStreamDestination();"
        " tone.connect(sink); tone.start(); ctx.resume();"
        " return sink.stream; };"
    )
    page_script = (
        "(async () => {"
        " const ctx = new AudioContext();"
        " const remote = ctx.createMediaStreamDestination();"
        " const tone = ctx.createOscillator(); tone.connect(remote); tone.start();"
        " ctx.createMediaStreamSource(remote.stream);"
        " const mic = await navigator.mediaDevices.getUserMedia({audio: true});"
        " ctx.createMediaStreamSource(new MediaStream([mic.getAudioTracks()[0].clone()]));"
        " ctx.resume();"
        "})();"
    )
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(fake_microphone)
        await page.add_init_script(load_voice_activity_script())
        await page.route(
            "https://voice.fakeyou.test/",
            lambda route: route.fulfill(
                body="<div data-participant-id='devices/1'>"
                "<div data-participant-id='devices/1'></div>"
                "<span class='notranslate'>Rani</span></div>"
                f"<script>{page_script}</script>",
                content_type="text/html",
            ),
        )
        await page.goto("https://voice.fakeyou.test/", wait_until="load")
        levels: list[list[float]] = []
        drained: dict[str, Any] = {}
        deadline = asyncio.get_running_loop().time() + 15.0
        while asyncio.get_running_loop().time() < deadline:
            drained = await page.evaluate(VOICE_ACTIVITY_DRAIN_JS)
            levels += drained["levels"]
            if drained["local"] and len([level for level in levels if level[2] > 0.1]) >= 10:
                break
            await asyncio.sleep(0.2)
        assert drained["local"] == [1]
        assert {int(slot) for _, slot, level in levels if level > 0.1} == {0}
        assert drained["participants"] == [{"id": "devices/1", "name": "Rani"}]
        assert isinstance(drained["instance"], str)
        assert drained["instance"]


@pytest.mark.browser
async def test_voice_script_reads_level_and_csrc_from_encoded_frames() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(load_voice_activity_script())
            await page.goto("about:blank")
            await page.evaluate(
                "async () => {"
                " const ctx = new AudioContext(); await ctx.resume();"
                " const tone = ctx.createOscillator();"
                " const out = ctx.createMediaStreamDestination();"
                " tone.connect(out); tone.start();"
                " const a = new RTCPeerConnection();"
                " const b = new RTCPeerConnection({encodedInsertableStreams: true});"
                " a.onicecandidate = (e) => e.candidate && b.addIceCandidate(e.candidate);"
                " b.onicecandidate = (e) => e.candidate && a.addIceCandidate(e.candidate);"
                " b.ontrack = (e) => {"
                "  const streams = e.receiver.createEncodedStreams();"
                "  streams.readable.pipeTo(streams.writable); };"
                " a.addTrack(out.stream.getAudioTracks()[0], out.stream);"
                " await a.setLocalDescription(await a.createOffer());"
                " await b.setRemoteDescription(a.localDescription);"
                " await b.setLocalDescription(await b.createAnswer());"
                " await a.setRemoteDescription(b.localDescription);"
                "}"
            )
            frames: list[list[Any]] = []
            deadline = asyncio.get_running_loop().time() + 15.0
            while len(frames) < 10 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                frames += (await page.evaluate(VOICE_ACTIVITY_DRAIN_JS))["frames"]
            assert len(frames) >= 10
            assert {frame[1] for frame in frames} == {0}
            assert max(frame[2] for frame in frames) > 0.1
            assert all(frame[3] == [] for frame in frames)
        finally:
            await browser.close()


@pytest.mark.browser
async def test_voice_script_releases_analysers_it_no_longer_samples() -> None:
    from playwright.async_api import async_playwright

    count_disconnects = (
        "window.__released = 0;"
        " const disconnect = AudioNode.prototype.disconnect;"
        " AudioNode.prototype.disconnect = function (...args) {"
        "  if (args[0] instanceof AnalyserNode) window.__released += 1;"
        "  return disconnect.apply(this, args); };"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.add_init_script(count_disconnects)
            await page.add_init_script(load_voice_activity_script())
            await page.goto("about:blank")
            released = await page.evaluate(
                "() => {"
                " const ctx = new AudioContext();"
                " const remote = ctx.createMediaStreamDestination();"
                " for (let i = 0; i < 20; i += 1) ctx.createMediaStreamSource(remote.stream);"
                " for (let i = 0; i < 20; i += 1)"
                "  ctx.createMediaStreamSource(ctx.createMediaStreamDestination().stream);"
                " return window.__released; }"
            )
            assert released == 19 + 5
        finally:
            await browser.close()


async def test_voice_activity_loop_reraises_unexpected_errors() -> None:
    tracker = tracker_at(FakeClock(now=10.0))
    page = meet_page([], evaluates=[KeyError("bug")])
    loop = VoiceActivityLoop(page, tracker)

    with pytest.raises(KeyError, match="bug"):
        await loop.poll()
