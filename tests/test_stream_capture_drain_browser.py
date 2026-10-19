import asyncio
import base64
import struct
from typing import override

import pytest

from fakeyou.audio.stream_capture import (
    load_capture_script,
)
from fakeyou.audio.voice_activity import SlotBinder
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from tests._fakes_common import STREAM_CAPTURE_TEST_FLAG

WINDOW_SAMPLES = 800


def peak_window_rms(pcm_b64: str) -> float:
    raw = base64.b64decode(pcm_b64)
    count = len(raw) // 2
    samples = struct.unpack(f"<{count}h", raw)
    peak = 0.0
    for start in range(0, count, WINDOW_SAMPLES):
        window = samples[start : start + WINDOW_SAMPLES]
        if not window:
            continue
        total = sum(sample * sample for sample in window)
        level = (total / len(window)) ** 0.5 / 32768.0
        peak = max(peak, level)
    return peak


LOOPBACK_TWO_TONES = (
    "async () => {"
    " window.__fakeyouLoopbackPairs = [];"
    " for (const freq of [440, 880]) {"
    "  const gen = new AudioContext(); await gen.resume();"
    "  const osc = gen.createOscillator(); osc.frequency.value = freq;"
    "  const gain = gen.createGain(); gain.gain.value = 1;"
    "  const dest = gen.createMediaStreamDestination();"
    "  osc.connect(gain); gain.connect(dest); osc.start();"
    "  const sender = new RTCPeerConnection();"
    "  const receiver = new RTCPeerConnection();"
    "  sender.onicecandidate = (e) => e.candidate && receiver.addIceCandidate(e.candidate);"
    "  receiver.onicecandidate = (e) => e.candidate && sender.addIceCandidate(e.candidate);"
    "  sender.addTrack(dest.stream.getAudioTracks()[0], dest.stream);"
    "  await sender.setLocalDescription(await sender.createOffer());"
    "  await receiver.setRemoteDescription(sender.localDescription);"
    "  await receiver.setLocalDescription(await receiver.createAnswer());"
    "  await sender.setRemoteDescription(receiver.localDescription);"
    "  window.__fakeyouLoopbackPairs.push({gen, osc, gain, dest, sender, receiver});"
    " }"
    "}"
)


LOOPBACK_ONE_TONE_CONSUME = (
    "async () => {"
    " window.__fakeyouConsumed = 0;"
    " window.__fakeyouConsumeError = null;"
    " const gen = new AudioContext(); await gen.resume();"
    " const osc = gen.createOscillator(); osc.frequency.value = 440;"
    " const gain = gen.createGain(); gain.gain.value = 1;"
    " const dest = gen.createMediaStreamDestination();"
    " osc.connect(gain); gain.connect(dest); osc.start();"
    " const sender = new RTCPeerConnection();"
    " const receiver = new RTCPeerConnection();"
    " receiver.ontrack = (event) => {"
    "  try {"
    "   const streams = event.receiver.createEncodedStreams();"
    "   const reader = streams.readable.getReader();"
    "   (async () => {"
    "    for (;;) {"
    "     const packet = await reader.read();"
    "     if (packet.done) return;"
    "     window.__fakeyouConsumed += 1;"
    "    }"
    "   })();"
    "  } catch (e) { window.__fakeyouConsumeError = String(e).slice(0, 120); }"
    " };"
    " sender.onicecandidate = (e) => e.candidate && receiver.addIceCandidate(e.candidate);"
    " receiver.onicecandidate = (e) => e.candidate && sender.addIceCandidate(e.candidate);"
    " sender.addTrack(dest.stream.getAudioTracks()[0], dest.stream);"
    " await sender.setLocalDescription(await sender.createOffer());"
    " await receiver.setRemoteDescription(sender.localDescription);"
    " await receiver.setLocalDescription(await receiver.createAnswer());"
    " await sender.setRemoteDescription(receiver.localDescription);"
    " window.__fakeyouConsumePair = {gen, osc, gain, dest, sender, receiver};"
    "}"
)


LOOPBACK_ONE_TONE_FORWARD = (
    "async () => {"
    " window.__fakeyouForwarded = 0;"
    " window.__fakeyouForwardError = null;"
    " const gen = new AudioContext(); await gen.resume();"
    " const osc = gen.createOscillator(); osc.frequency.value = 440;"
    " const gain = gen.createGain(); gain.gain.value = 1;"
    " const dest = gen.createMediaStreamDestination();"
    " osc.connect(gain); gain.connect(dest); osc.start();"
    " const sender = new RTCPeerConnection();"
    " const receiver = new RTCPeerConnection();"
    " receiver.ontrack = (event) => {"
    "  try {"
    "   const streams = event.receiver.createEncodedStreams();"
    "   (async () => {"
    "    const reader = streams.readable.getReader();"
    "    const writer = streams.writable.getWriter();"
    "    for (;;) {"
    "     const packet = await reader.read();"
    "     if (packet.done) return;"
    "     window.__fakeyouForwarded += 1;"
    "     await writer.write(packet.value);"
    "    }"
    "   })().catch((e) => { window.__fakeyouForwardError = String(e).slice(0, 120); });"
    "  } catch (e) { window.__fakeyouForwardError = String(e).slice(0, 120); }"
    " };"
    " sender.onicecandidate = (e) => e.candidate && receiver.addIceCandidate(e.candidate);"
    " receiver.onicecandidate = (e) => e.candidate && sender.addIceCandidate(e.candidate);"
    " sender.addTrack(dest.stream.getAudioTracks()[0], dest.stream);"
    " await sender.setLocalDescription(await sender.createOffer());"
    " await receiver.setRemoteDescription(sender.localDescription);"
    " await receiver.setLocalDescription(await receiver.createAnswer());"
    " await sender.setRemoteDescription(receiver.localDescription);"
    " window.__fakeyouForwardPair = {gen, osc, gain, dest, sender, receiver};"
    "}"
)


FAIL_DECODER_STUB = (
    "() => {"
    " window.__fakeyouDecodeCalls = 0;"
    " window.AudioDecoder = class {"
    "  configure() {}"
    "  decode() { window.__fakeyouDecodeCalls += 1; throw new Error('forced failure'); }"
    "  close() {}"
    " };"
    "}"
)


PATCH_RECEIVER_SOURCES = (
    "() => {"
    " const syncs = [6666, 6667];"
    " const byTrack = new Map();"
    " let next = 0;"
    " const syncFor = (id) => {"
    "  if (!byTrack.has(id)) byTrack.set(id, syncs[next++ % syncs.length]);"
    "  return byTrack.get(id);"
    " };"
    " try {"
    "  const proto = RTCRtpReceiver.prototype;"
    "  proto.getContributingSources = function () { return [{source: 3707908267}]; };"
    "  proto.getSynchronizationSources = function () {"
    "   const id = this.track ? String(this.track.id) : '';"
    "   return [{source: syncFor(id)}];"
    "  };"
    " } catch (e) { void e; }"
    " for (const pair of window.__fakeyouLoopbackPairs || []) {"
    "  const found = pair.receiver.getReceivers ? pair.receiver.getReceivers() : [];"
    "  for (const receiver of found) {"
    "   if (!receiver.track || receiver.track.kind !== 'audio') continue;"
    "   const sync = syncFor(String(receiver.track.id));"
    "   receiver.getContributingSources = () => [{source: 3707908267}];"
    "   receiver.getSynchronizationSources = () => [{source: sync}];"
    "  }"
    " }"
    "}"
)


def test_load_capture_script_exposes_capture_api() -> None:
    script = load_capture_script()
    assert "window.__fakeyouStreamCapture" in script
    assert "attachTestTones" in script
    assert "drain" in script


def test_capture_script_gates_test_hooks_behind_a_flag() -> None:
    script = load_capture_script()

    assert "__fakeyouTestHooks" in script
    assert "window.__fakeyouTestHooks === true" in script


@pytest.mark.browser
async def test_capture_api_exposes_only_drain_and_pending_without_flag() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        keys = await page.evaluate("() => Object.keys(window.__fakeyouStreamCapture)")

    assert sorted(keys) == ["drain", "pending"]


@pytest.mark.browser
async def test_capture_api_exposes_test_hooks_with_flag() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        keys = await page.evaluate("() => Object.keys(window.__fakeyouStreamCapture)")

    assert "attachTestTones" in keys
    assert "drain" in keys


@pytest.mark.browser
async def test_stream_capture_separates_two_tones() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(2.0)
        pending = await page.evaluate("() => window.__fakeyouStreamCapture.pending()")
        assert isinstance(pending, int)
        assert pending > 0
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    assert len(drained["streams"]) == 2
    assert drained["streams"][0]["pcm_b64"]
    assert drained["streams"][0]["slot"] != drained["streams"][1]["slot"]


@pytest.mark.browser
async def test_stream_capture_muting_one_tone_keeps_other_stream() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        baseline = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        assert len(baseline["streams"]) == 2
        before = {entry["slot"]: peak_window_rms(entry["pcm_b64"]) for entry in baseline["streams"]}
        assert before[0] > 0.01
        assert before[1] > 0.01
        await page.evaluate("() => window.__fakeyouStreamCapture.setTestTone(0, 0.0)")
        await asyncio.sleep(0.3)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(1.0)
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    after = {entry["slot"]: peak_window_rms(entry["pcm_b64"]) for entry in drained["streams"]}
    assert after[0] < before[0] * 0.2
    assert after[1] > before[1] * 0.5


@pytest.mark.browser
async def test_stream_capture_drain_emits_monotonic_seq_per_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        first = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.5)
        second = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    first_seq = {entry["slot"]: entry["seq"] for entry in first["streams"]}
    second_seq = {entry["slot"]: entry["seq"] for entry in second["streams"]}
    assert first_seq == {0: 0, 1: 0}
    assert second_seq == {0: 1, 1: 1}


@pytest.mark.browser
async def test_stream_capture_discovers_loopback_receivers() -> None:
    from playwright.async_api import async_playwright

    script = load_capture_script()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
            ignore_default_args=["--mute-audio"],
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
            await page.add_init_script(script)
            await page.goto("about:blank")
            await page.evaluate(LOOPBACK_TWO_TONES)
            deadline = asyncio.get_running_loop().time() + 15.0
            pending = 0
            while pending < 4 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                pending = await page.evaluate("() => window.__fakeyouStreamCapture.pending()")
            assert pending >= 4
            drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
            states = await page.evaluate(
                "() => window.__fakeyouLoopbackPairs.map"
                "((pair) => pair.receiver.iceConnectionState)"
            )
        finally:
            await browser.close()
    assert len(states) == 2
    assert all(state in ("connected", "completed") for state in states)
    assert len(drained["streams"]) == 2
    assert drained["streams"][0]["slot"] != drained["streams"][1]["slot"]
    for entry in drained["streams"]:
        assert entry["pcm_b64"]
        assert entry["level"] > 0.01


@pytest.mark.browser
async def test_stream_capture_drain_retaps_released_receiver() -> None:
    from playwright.async_api import async_playwright

    script = load_capture_script()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
            ignore_default_args=["--mute-audio"],
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
            await page.add_init_script(script)
            await page.goto("about:blank")
            await page.evaluate(LOOPBACK_TWO_TONES)
            deadline = asyncio.get_running_loop().time() + 15.0
            pending = 0
            while pending < 4 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                pending = await page.evaluate("() => window.__fakeyouStreamCapture.pending()")
            assert pending >= 4
            track_id = await page.evaluate(
                "() => window.__fakeyouLoopbackPairs[0].receiver.getReceivers()[0].track.id"
            )
            await page.evaluate(
                f"() => window.__fakeyouStreamCapture.releaseTestTrack({track_id!r})"
            )
            await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
            await asyncio.sleep(1.0)
            drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        finally:
            await browser.close()
    assert len(drained["streams"]) == 2
    for entry in drained["streams"]:
        assert entry["level"] > 0.01


@pytest.mark.browser
async def test_stream_capture_consume_setup_decodes_audio() -> None:
    import http.server
    import threading

    from playwright.async_api import async_playwright

    script = load_capture_script()

    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<!doctype html><title>tap</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, kind: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
                ignore_default_args=["--mute-audio"],
            )
            try:
                page = await browser.new_page()
                await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
                await page.add_init_script(script)
                await page.goto(url)
                await page.evaluate(LOOPBACK_ONE_TONE_CONSUME)
                deadline = asyncio.get_running_loop().time() + 15.0
                state = "?"
                while asyncio.get_running_loop().time() < deadline:
                    state = await page.evaluate(
                        "() => window.__fakeyouConsumePair.receiver.iceConnectionState"
                    )
                    if state in ("connected", "completed"):
                        break
                    await asyncio.sleep(0.3)
                assert state in ("connected", "completed")
                await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
                await asyncio.sleep(3.0)
                consumed = await page.evaluate("() => window.__fakeyouConsumed")
                error = await page.evaluate("() => window.__fakeyouConsumeError")
                drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
            finally:
                await browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert error is None
    assert consumed > 0
    assert len(drained["streams"]) == 1
    assert drained["streams"][0]["level"] > 0.01


@pytest.mark.browser
async def test_stream_capture_failing_decoder_still_drains_live_mstp() -> None:
    import http.server
    import threading

    from playwright.async_api import async_playwright

    script = load_capture_script()

    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = b"<!doctype html><title>tap</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, kind: str, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
                ignore_default_args=["--mute-audio"],
            )
            try:
                page = await browser.new_page()
                await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
                await page.add_init_script(script)
                await page.goto(url)
                await page.evaluate(FAIL_DECODER_STUB)
                await page.evaluate(LOOPBACK_ONE_TONE_FORWARD)
                deadline = asyncio.get_running_loop().time() + 15.0
                state = "?"
                while asyncio.get_running_loop().time() < deadline:
                    state = await page.evaluate(
                        "() => window.__fakeyouForwardPair.receiver.iceConnectionState"
                    )
                    if state in ("connected", "completed"):
                        break
                    await asyncio.sleep(0.3)
                assert state in ("connected", "completed")
                await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
                await asyncio.sleep(3.0)
                calls = await page.evaluate("() => window.__fakeyouDecodeCalls")
                forwarded = await page.evaluate("() => window.__fakeyouForwarded")
                error = await page.evaluate("() => window.__fakeyouForwardError")
                drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
            finally:
                await browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert error is None
    assert calls > 0
    assert forwarded > 0
    assert len(drained["streams"]) == 1
    assert drained["streams"][0]["level"] > 0.01


@pytest.mark.browser
async def test_stream_capture_reports_sync_sources_without_mixer_overlap() -> None:
    from playwright.async_api import async_playwright

    script = load_capture_script()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required"],
            ignore_default_args=["--mute-audio"],
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
            await page.add_init_script(script)
            await page.goto("about:blank")
            await page.evaluate(LOOPBACK_TWO_TONES)
            deadline = asyncio.get_running_loop().time() + 15.0
            pending = 0
            while pending < 4 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                pending = await page.evaluate("() => window.__fakeyouStreamCapture.pending()")
            assert pending >= 4
            await page.evaluate(PATCH_RECEIVER_SOURCES)
            await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
            await asyncio.sleep(1.0)
            drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        finally:
            await browser.close()
    assert len(drained["streams"]) == 2
    csrcs = [tuple(entry["csrcs"]) for entry in drained["streams"]]
    assert all(3707908267 not in found for found in csrcs)
    assert csrcs[0]
    assert csrcs[1]
    assert set(csrcs[0]).isdisjoint(csrcs[1])
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    for entry in drained["streams"]:
        binder.observe_stream(entry["slot"], entry["csrcs"])
    first = binder.resolve(drained["streams"][0]["slot"])
    second = binder.resolve(drained["streams"][1]["slot"])
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.label != second.label
    kept = binder.resolve(drained["streams"][0]["slot"])
    assert not isinstance(kept, str)
    assert kept.label == first.label
