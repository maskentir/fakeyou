import asyncio
import base64
import json
import struct
from collections.abc import Callable
from pathlib import Path

import pytest
from pipecat.transports.base_transport import TransportParams

from fakeyou.browser import open_direct_page
from fakeyou.stream_capture import StreamAudioInput, StreamRegistry, load_capture_script
from fakeyou.voice_activity import SlotBinder
from tests.fakes import FakeClock, meet_page

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


class _RecordingInput(StreamAudioInput):
    def __init__(self) -> None:
        super().__init__(TransportParams(audio_in_enabled=True))
        self.fed: list[bytes] = []

    async def feed(self, audio: bytes, sample_rate: int = 16000) -> None:
        self.fed.append(audio)


def _recording_registry(
    monotonic: Callable[[], float],
    binder: SlotBinder | None = None,
) -> tuple[StreamRegistry, list[_RecordingInput]]:
    made: list[_RecordingInput] = []

    def _factory() -> StreamAudioInput:
        created = _RecordingInput()
        made.append(created)
        return created

    return StreamRegistry(monotonic=monotonic, input_factory=_factory, binder=binder), made


def test_load_capture_script_exposes_capture_api() -> None:
    script = load_capture_script()
    assert "window.__fakeyouStreamCapture" in script
    assert "attachTestTones" in script
    assert "drain" in script


@pytest.mark.browser
async def test_stream_capture_separates_two_tones() -> None:
    script = load_capture_script()
    async with open_direct_page(headless=True) as page:
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
    async with open_direct_page(headless=True) as page:
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


async def test_stream_input_tags_transcripts_with_stream_id() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.transports.base_transport import TransportParams

    from fakeyou.audio import open_stream_audio
    from fakeyou.stream_capture import StreamAudioInput

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

    class _Playback:
        def __init__(self) -> None:
            self.closed = False

        @property
        def is_speaking(self) -> bool:
            return False

        async def speak(self, text: str, **extra: object) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def close(self) -> None:
            self.closed = True

    left: list[tuple[str, str]] = []
    right: list[tuple[str, str]] = []
    left_ready = asyncio.Event()
    right_ready = asyncio.Event()

    async def on_left(frame: TranscriptionFrame) -> None:
        left.append((frame.transport_source or "", frame.text))
        left_ready.set()

    async def on_right(frame: TranscriptionFrame) -> None:
        right.append((frame.transport_source or "", frame.text))
        right_ready.set()

    assert callable(open_stream_audio)
    assert callable(StreamAudioInput)

    left_input = StreamAudioInput(TransportParams(audio_in_enabled=True))
    right_input = StreamAudioInput(TransportParams(audio_in_enabled=True))
    async with open_stream_audio(
        stream_id="slot-0",
        input=left_input,
        on_transcript=on_left,
        stt=_EchoSTT("left speech"),
        playback=_Playback(),
    ):
        async with open_stream_audio(
            stream_id="slot-1",
            input=right_input,
            on_transcript=on_right,
            stt=_EchoSTT("right speech"),
            playback=_Playback(),
        ):
            await left_input.feed(b"\x01\x02" * 160)
            await right_input.feed(b"\x03\x04" * 160)
            await asyncio.wait_for(left_ready.wait(), timeout=5)
            await asyncio.wait_for(right_ready.wait(), timeout=5)

    assert left == [("slot-0", "left speech")]
    assert right == [("slot-1", "right speech")]


def test_parse_drain_decodes_pcm_windows() -> None:
    from fakeyou.stream_capture import parse_drain

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [111], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [],
        "dropped": 0,
    }
    (chunk,) = parse_drain(payload)
    assert chunk.slot == 0 and chunk.csrcs == (111,) and len(chunk.pcm) == 4


@pytest.mark.parametrize(
    "payload",
    [
        {"roster": [], "dropped": 0},
        {"streams": {}, "roster": [], "dropped": 0},
        {"streams": ["nope"], "roster": [], "dropped": 0},
        {
            "streams": [
                {
                    "csrcs": [111],
                    "level": 0.4,
                    "page_ms": 1000.0,
                    "pcm_b64": "AAP//w==",
                }
            ],
            "roster": [],
            "dropped": 0,
        },
        {
            "streams": [
                {
                    "slot": 0,
                    "csrcs": [111],
                    "level": 0.4,
                    "page_ms": 1000.0,
                }
            ],
            "roster": [],
            "dropped": 0,
        },
        {
            "streams": [
                {
                    "slot": 0,
                    "csrcs": [111],
                    "level": 0.4,
                    "page_ms": 1000.0,
                    "pcm_b64": "!!!",
                }
            ],
            "roster": [],
            "dropped": 0,
        },
        {
            "streams": [
                {
                    "slot": 0,
                    "csrcs": [111],
                    "level": 0.4,
                    "page_ms": 1000.0,
                    "pcm_b64": "AA==",
                }
            ],
            "roster": [],
            "dropped": 0,
        },
        {
            "streams": [
                {
                    "slot": "0",
                    "csrcs": [111],
                    "level": 0.4,
                    "page_ms": 1000.0,
                    "pcm_b64": "AAP//w==",
                }
            ],
            "roster": [],
            "dropped": 0,
        },
        {
            "streams": [
                {
                    "slot": 0,
                    "csrcs": [111.5],
                    "level": 0.4,
                    "page_ms": 1000.0,
                    "pcm_b64": "AAP//w==",
                }
            ],
            "roster": [],
            "dropped": 0,
        },
    ],
)
def test_parse_drain_rejects_corrupt_windows(payload: dict[str, object]) -> None:
    from typing import Any, cast

    from fakeyou.audio import MeetingAudioError
    from fakeyou.stream_capture import parse_drain

    with pytest.raises(MeetingAudioError):
        parse_drain(cast(dict[str, Any], payload))


async def test_registry_forwards_pcm_to_slot_inputs() -> None:
    from fakeyou.stream_capture import StreamChunk

    registry, made = _recording_registry(FakeClock().monotonic)
    await registry.feed(
        StreamChunk(slot=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x00\x01")
    )
    await registry.feed(
        StreamChunk(slot=1, csrcs=(222,), level=0.5, page_ms=1000.0, pcm=b"\x02\x03")
    )
    await registry.feed(
        StreamChunk(slot=0, csrcs=(111,), level=0.6, page_ms=1250.0, pcm=b"\x04\x05")
    )

    assert len(made) == 2
    assert made[0].fed == [b"\x00\x01", b"\x04\x05"]
    assert made[1].fed == [b"\x02\x03"]
    assert set(registry.sessions) == {0, 1}
    assert registry.sessions[0].level == 0.6
    assert registry.sessions[0].page_ms == 1250.0


async def test_registry_releases_session_after_silence() -> None:
    from fakeyou.stream_capture import StreamChunk

    clock = FakeClock(now=1000.0)
    registry, _ = _recording_registry(clock.monotonic)
    chunk = StreamChunk(slot=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x00\x01")
    await registry.feed(chunk)
    assert set(registry.sessions) == {0}
    clock.now += 29.0
    registry.prune()
    assert set(registry.sessions) == {0}
    clock.now += 2.0
    registry.prune()
    assert dict(registry.sessions) == {}
    await registry.feed(chunk)
    assert set(registry.sessions) == {0}


async def test_drain_throughput_within_budget() -> None:
    import base64
    import time

    from fakeyou.stream_capture import parse_drain

    pcm_b64 = base64.b64encode(bytes(8000)).decode("ascii")
    payload = {
        "streams": [
            {
                "slot": slot,
                "csrcs": [100 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": pcm_b64,
            }
            for slot in range(4)
        ],
        "roster": [],
        "dropped": 0,
    }
    registry, _ = _recording_registry(time.monotonic)
    started = time.monotonic()
    for _ in range(240):
        for chunk in parse_drain(payload):
            await registry.feed(chunk)
    assert time.monotonic() - started < 5.0


async def test_capture_drain_loop_polls_and_feeds_registry() -> None:
    from fakeyou.stream_capture import CAPTURE_DRAIN_JS, CaptureDrainLoop

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [111], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload, None])
    registry, made = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry)
    assert await loop.poll() is True
    assert await loop.poll() is False
    assert page.evaluate_calls == [CAPTURE_DRAIN_JS, CAPTURE_DRAIN_JS]
    assert set(registry.sessions) == {0}
    assert registry.sessions[0].csrcs == (111,)
    assert made[0].fed == [b"\x00\x03\xff\xff"]


async def test_start_capture_drain_respects_enabled_flag() -> None:
    import asyncio

    from langgraph.checkpoint.memory import InMemorySaver

    from fakeyou.lifecycle import MeetingLifecycle
    from fakeyou.meet import DEFAULT_MEET_SIGNALS
    from fakeyou.runtime import _MeetingWorker
    from fakeyou.session import SessionConfig
    from tests.fakes import LINK

    clock = FakeClock()
    page = meet_page([])
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    worker = _MeetingWorker(
        SessionConfig(
            owner_id="owner-1",
            twin_id="twin-1",
            meeting_id="meeting-1",
            link=LINK,
            twin_name="Twin",
        ),
        lifecycle,
        page,
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    await worker.start_capture_drain(enabled=False)
    assert worker.capture_drain is None
    await worker.start_capture_drain(enabled=True)
    assert worker.capture_drain is not None
    await worker.close_capture_drain()
    assert worker.capture_drain is None


def _frame(text: str, timestamp: str):
    from pipecat.frames.frames import TranscriptionFrame

    return TranscriptionFrame(text, user_id="", timestamp=timestamp)


def test_merge_orders_two_streams_chronologically() -> None:
    from fakeyou.conversation import merge_transcripts, transcript_event

    first = transcript_event(_frame("hello", "2026-10-06T01:00:02+00:00"), "a", None, "s1")
    second = transcript_event(_frame("hi", "2026-10-06T01:00:01+00:00"), "b", None, "s0")
    assert [event.text for event in merge_transcripts([first, second])] == ["hi", "hello"]


def test_merge_keeps_stream_tags_stable_on_id() -> None:
    from fakeyou.conversation import merge_transcripts, transcript_event

    first = transcript_event(_frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, "s1")
    second = transcript_event(_frame("hi", "2026-10-06T01:00:01+00:00"), "b", None, "s0")
    merged = merge_transcripts([second, first])
    assert [event.id for event in merged] == sorted([first.id, second.id])
    assert {event.stream_id for event in merged} == {"s0", "s1"}


def test_binder_follows_csrc_across_slot_rotation() -> None:
    from fakeyou.voice_activity import SlotBinder

    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    first = binder.resolve(0)
    second = binder.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.label != second.label
    binder.observe_stream(2, [111])
    rotated = binder.resolve(2)
    assert not isinstance(rotated, str)
    assert rotated.label == first.label


def test_flag_off_keeps_mixed_path() -> None:
    from fakeyou.settings import Settings

    assert Settings().conversation.per_stream_enabled is False


def test_stream_ids_change_transcript_identity() -> None:
    from fakeyou.conversation import transcript_event

    left = transcript_event(_frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, "s0")
    replay = transcript_event(_frame("hello", "2026-10-06T01:00:01+00:00"), "b", None, "s0")
    other = transcript_event(_frame("hello", "2026-10-06T01:00:01+00:00"), "a", None, "s1")
    assert left.id == replay.id
    assert left.id != other.id


def test_request_messages_carry_stream_id() -> None:
    from fakeyou.conversation import _request_messages, transcript_event

    event = transcript_event(_frame("hello", "2026-10-06T01:00:01+00:00"), "a", "e1", "slot-2")
    (message,) = _request_messages([event])
    assert message.additional_kwargs["meeting_event"]["stream_id"] == "slot-2"


def test_record_stores_stream_id(tmp_path: Path) -> None:
    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.conversation import transcript_event
    from fakeyou.record import (
        MeetingRecord,
        TranscriptEntry,
        load_meeting_record,
        meeting_record_path,
    )

    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path)
    frame = TranscriptionFrame("hello", "", "2026-10-06T01:00:01+00:00", finalized=True)
    record.add_transcript(transcript_event(frame, "a", "e1", "slot-1"))
    record.close()
    [entry] = load_meeting_record(path)
    assert isinstance(entry, TranscriptEntry)
    assert entry.stream_id == "slot-1"
    legacy = {
        "kind": "transcript",
        "at": "2026-10-06T01:00:02+00:00",
        "id": "event-legacy",
        "spoken_at": "2026-10-06T01:00:02+00:00",
        "source": "meeting_audio",
        "text": "Legacy.",
        "participant_id": "unknown",
        "speaker_evidence_id": None,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(legacy) + "\n")
    entries = load_meeting_record(path)
    assert isinstance(entries[1], TranscriptEntry)
    assert entries[1].stream_id == ""


async def test_stream_input_buffers_audio_before_start() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.transports.base_transport import TransportParams

    from fakeyou.audio import open_stream_audio
    from fakeyou.stream_capture import StreamAudioInput

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

    class _Playback:
        @property
        def is_speaking(self) -> bool:
            return False

        async def speak(self, text: str, **extra: object) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def close(self) -> None:
            pass

    seen: list[tuple[str, str]] = []
    ready = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        ready.set()

    streamer = StreamAudioInput(TransportParams(audio_in_enabled=True))
    await streamer.feed(b"\x05\x06" * 160)
    async with open_stream_audio(
        stream_id="slot-9",
        input=streamer,
        on_transcript=on_transcript,
        stt=_EchoSTT("early speech"),
        playback=_Playback(),
    ):
        await asyncio.wait_for(ready.wait(), timeout=5)
    assert seen == [("slot-9", "early speech")]


async def test_registry_observes_streams_and_roster_into_binder() -> None:
    from fakeyou.stream_capture import StreamChunk
    from fakeyou.voice_activity import SlotBinder

    clock = FakeClock()
    binder = SlotBinder()
    registry, _ = _recording_registry(clock.monotonic, binder)
    registry.update_roster(["host", "guest"])
    await registry.feed(
        StreamChunk(slot=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x00\x01")
    )
    await registry.feed(
        StreamChunk(slot=1, csrcs=(222,), level=0.5, page_ms=1000.0, pcm=b"\x02\x03")
    )
    first = binder.resolve(0)
    second = binder.resolve(1)
    assert not isinstance(first, str)
    assert not isinstance(second, str)
    assert first.label != second.label
    clock.now += 1.0
    registry.update_roster(["host"])
    kept = binder.resolve(0)
    assert not isinstance(kept, str)
    assert kept.label == first.label
    clock.now += 3.0
    registry.update_roster(["host"])
    survivor = binder.resolve(1)
    assert not isinstance(survivor, str)
    assert survivor.label == second.label
    assert survivor.device == "host"
    assert binder.resolve(0) == "slot_conflict"


async def test_capture_drain_poll_updates_binder_roster() -> None:
    from fakeyou.stream_capture import CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [111], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [
            {"id": "host", "name": "Host"},
            {"id": "", "name": "Ghost"},
            "broken",
            {"name": "NoId"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder()
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry)
    assert await loop.poll() is True
    outcome = binder.resolve(0)
    assert not isinstance(outcome, str)
    assert outcome.device == "host"


async def test_capture_drain_loop_emits_level_heartbeat() -> None:
    import json

    from fakeyou.journal import Journal
    from fakeyou.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
                "track": "abc123",
            },
            {
                "slot": 1,
                "csrcs": [],
                "level": 0.0,
                "page_ms": 1000.0,
                "pcm_b64": "AAA=",
                "track": "def456",
            },
        ],
        "roster": [],
        "dropped": 2,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, stdout=False)
    page = meet_page([], evaluates=[payload, payload, payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry, journal=journal, heartbeat_polls=3)
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert lines == []
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["dropped"] == 2
    assert event["fails"] == 0
    assert event["slots"]["0"] == {"level": 0.4, "bytes": 4, "track": "abc123"}
    assert event["slots"]["1"] == {"level": 0.0, "bytes": 2, "track": "def456"}


async def test_flag_off_record_matches_stream_mode_except_identity(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.journal import Journal
    from fakeyou.record import MeetingRecord, meeting_record_path
    from fakeyou.voice_activity import SlotBinder

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    turns = [
        TranscriptionFrame(
            "Let us start with the demo.", "", "2026-10-06T01:00:01+00:00", finalized=True
        ),
        TranscriptionFrame(
            "Sounds good, sharing now.", "", "2026-10-06T01:00:02+00:00", finalized=True
        ),
    ]

    class _Conversation:
        def __init__(self) -> None:
            self.events: list = []

        async def handle_transcript(self, event) -> None:
            self.events.append(event)

    async def run(per_stream: bool, tag: str):
        lines: list[str] = []
        journal = Journal("meeting-9", emit=lines.append, now=lambda: now, stdout=False)
        path = meeting_record_path(tmp_path, tag)
        record = MeetingRecord(path, now=lambda: now)
        conversation = _Conversation()
        callback = build_transcript_callback(
            journal=journal,
            record=record,
            conversation=conversation,
            voice_activity=None,
            binder=SlotBinder(),
            per_stream_enabled=per_stream,
        )
        for index, frame in enumerate(turns):
            if per_stream:
                frame.transport_source = f"slot-{index}"
            await callback(frame)
        record.close()
        journal.close()
        rows = [
            row for line in lines if (row := json.loads(line))["event"] == "transcript_finalized"
        ]
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return rows, entries, conversation.events

    mixed_rows, mixed_entries, mixed_events = await run(False, "mixed-9")
    stream_rows, stream_entries, stream_events = await run(True, "stream-9")
    assert [event.text for event in mixed_events] == [event.text for event in stream_events]
    assert [event.text for event in mixed_events] == [
        "Let us start with the demo.",
        "Sounds good, sharing now.",
    ]
    assert [entry["stream_id"] for entry in mixed_entries] == ["", ""]
    assert [entry["stream_id"] for entry in stream_entries] == ["slot-0", "slot-1"]
    assert [
        {key: value for key, value in entry.items() if key not in ("id", "stream_id")}
        for entry in mixed_entries
    ] == [
        {key: value for key, value in entry.items() if key not in ("id", "stream_id")}
        for entry in stream_entries
    ]
    stream_diagnostics = ("speaker_reason", "speaker_slot", "speaker_bound", "roster_size")
    assert [{key: row[key] for key in stream_diagnostics} for row in stream_rows] == [
        {
            "speaker_reason": "no_roster",
            "speaker_slot": 0,
            "speaker_bound": False,
            "roster_size": 0,
        },
        {
            "speaker_reason": "no_roster",
            "speaker_slot": 1,
            "speaker_bound": False,
            "roster_size": 0,
        },
    ]
    assert {
        frozenset(key for key in row if key not in stream_diagnostics) for row in mixed_rows
    } == {frozenset(key for key in row if key not in stream_diagnostics) for row in stream_rows}
    assert [
        {
            key: value
            for key, value in row.items()
            if key not in ("transcript_id", "stream_id", *stream_diagnostics)
        }
        for row in mixed_rows
    ] == [
        {
            key: value
            for key, value in row.items()
            if key not in ("transcript_id", "stream_id", *stream_diagnostics)
        }
        for row in stream_rows
    ]


async def test_stream_callback_attributes_via_binder(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.conversation import TranscriptEvent
    from fakeyou.journal import Journal
    from fakeyou.record import MeetingRecord, meeting_record_path
    from fakeyou.voice_activity import SlotBinder

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    binder = SlotBinder()
    binder.update_roster(["host", "guest"])
    binder.observe_stream(0, [111])
    binder.observe_stream(1, [222])
    frames = [
        TranscriptionFrame("Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True),
        TranscriptionFrame(
            "Thanks, glad to join.", "", "2026-10-06T01:00:02+00:00", finalized=True
        ),
    ]
    frames[0].transport_source = "slot-0"
    frames[1].transport_source = "slot-1"

    seen: list[TranscriptEvent] = []

    class _Conversation:
        async def handle_transcript(self, event: TranscriptEvent) -> None:
            seen.append(event)

    journal = Journal("meeting-9", emit=lambda line: None, now=lambda: now, stdout=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=_Conversation(),
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
    )
    for frame in frames:
        await callback(frame)
    record.close()
    journal.close()
    assert [event.participant_id for event in seen] == ["speaker-a", "speaker-b"]
    assert [event.speaker_evidence_id for event in seen] == ["stream-0", "stream-1"]
    assert [event.stream_id for event in seen] == ["slot-0", "slot-1"]
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [entry["participant_id"] for entry in entries] == ["speaker-a", "speaker-b"]
    assert [entry["stream_id"] for entry in entries] == ["slot-0", "slot-1"]


async def test_stream_fleet_tags_transcripts_per_slot() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from fakeyou.stream_capture import StreamChunk, open_stream_fleet
    from fakeyou.voice_activity import SlotBinder

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

    class _Playback:
        @property
        def is_speaking(self) -> bool:
            return False

        async def speak(self, text: str, **extra: object) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def close(self) -> None:
            pass

    seen: list[tuple[str, str]] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        if len(seen) == 2:
            received.set()

    async with open_stream_fleet(
        slots=2,
        binder=SlotBinder(),
        on_transcript=on_transcript,
        stt_factory=lambda slot: _EchoSTT(f"speech-{slot}"),
        playback_factory=lambda slot: _Playback(),
    ) as fleet:
        assert set(fleet.capture_registry.sessions) == {0, 1}
        assert fleet.capture_registry.sessions[0].pinned is True
        await fleet.capture_registry.feed(
            StreamChunk(slot=0, csrcs=(111,), level=0.4, page_ms=1000.0, pcm=b"\x01\x02" * 160)
        )
        await fleet.capture_registry.feed(
            StreamChunk(slot=1, csrcs=(222,), level=0.5, page_ms=1000.0, pcm=b"\x03\x04" * 160)
        )
        await asyncio.wait_for(received.wait(), timeout=10)
    assert sorted(seen) == [("slot-0", "speech-0"), ("slot-1", "speech-1")]


async def test_stream_fleet_covers_fourth_slot() -> None:
    from pipecat.frames.frames import AudioRawFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, StreamChunk, open_stream_fleet
    from fakeyou.voice_activity import SlotBinder

    class _EchoSTT(FrameProcessor):
        def __init__(self, text: str) -> None:
            super().__init__()
            self._text = text

        async def process_frame(self, frame, direction: FrameDirection) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, AudioRawFrame):
                await self.push_frame(
                    TranscriptionFrame(self._text, "guest", "", finalized=True), direction
                )
            await self.push_frame(frame, direction)

    class _Playback:
        @property
        def is_speaking(self) -> bool:
            return False

        async def speak(self, text: str, **extra: object) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def close(self) -> None:
            pass

    assert STREAM_FLEET_SLOTS == 4
    seen: list[tuple[str, str]] = []
    received = asyncio.Event()

    async def on_transcript(frame: TranscriptionFrame) -> None:
        seen.append((frame.transport_source or "", frame.text))
        if len(seen) == 4:
            received.set()

    async with open_stream_fleet(
        slots=STREAM_FLEET_SLOTS,
        binder=SlotBinder(),
        on_transcript=on_transcript,
        stt_factory=lambda slot: _EchoSTT(f"speech-{slot}"),
        playback_factory=lambda slot: _Playback(),
    ) as fleet:
        assert set(fleet.capture_registry.sessions) == {0, 1, 2, 3}
        for slot in range(4):
            await fleet.capture_registry.feed(
                StreamChunk(
                    slot=slot,
                    csrcs=(100 + slot,),
                    level=0.4,
                    page_ms=1000.0,
                    pcm=b"\x01\x02" * 160,
                )
            )
        await asyncio.wait_for(received.wait(), timeout=10)
    assert sorted(seen) == [(f"slot-{slot}", f"speech-{slot}") for slot in range(4)]


async def test_start_capture_drain_uses_fleet_registry() -> None:
    import asyncio
    from types import SimpleNamespace

    from langgraph.checkpoint.memory import InMemorySaver

    from fakeyou.lifecycle import MeetingLifecycle
    from fakeyou.meet import DEFAULT_MEET_SIGNALS
    from fakeyou.runtime import _MeetingWorker
    from fakeyou.session import SessionConfig
    from fakeyou.stream_capture import StreamRegistry
    from tests.fakes import LINK

    clock = FakeClock()
    page = meet_page([])
    lifecycle = MeetingLifecycle("owner-1", "twin-1", "meeting-1", InMemorySaver())
    await lifecycle.initialize()
    worker = _MeetingWorker(
        SessionConfig(
            owner_id="owner-1",
            twin_id="twin-1",
            meeting_id="meeting-1",
            link=LINK,
            twin_name="Twin",
        ),
        lifecycle,
        page,
        asyncio.Event(),
        DEFAULT_MEET_SIGNALS,
        clock.sleep,
        clock.monotonic,
    )
    registry = StreamRegistry()
    worker.audio = SimpleNamespace(capture_registry=registry)
    await worker.start_capture_drain(enabled=True)
    assert worker.capture_registry is registry
    await worker.close_capture_drain()
    assert worker.capture_registry is None


async def test_fleet_alone_requires_every_slot_alone() -> None:
    from fakeyou.stream_capture import StreamFleetSession, StreamRegistry

    class _Slot:
        def __init__(self, verdict: str) -> None:
            self._verdict = verdict

        def raise_if_failed(self) -> None:
            pass

        async def maintenance_tick(self, **extra: object) -> str:
            return self._verdict

    mixed = StreamFleetSession([_Slot("alone"), _Slot("hearing")], StreamRegistry())
    assert await mixed.maintenance_tick(is_busy=False) == "hearing"
    unanimous = StreamFleetSession([_Slot("alone"), _Slot("alone")], StreamRegistry())
    assert await unanimous.maintenance_tick(is_busy=False) == "alone"


async def test_fleet_speak_raises_when_every_slot_dead() -> None:
    from fakeyou.audio import MeetingAudioError
    from fakeyou.stream_capture import StreamFleetSession, StreamRegistry

    class _DeadSlot:
        spoken: list[str] = []

        def raise_if_failed(self) -> None:
            raise MeetingAudioError("slot is dead")

        async def speak(self, text: str, **extra: object) -> None:
            self.spoken.append(text)

    fleet = StreamFleetSession([_DeadSlot(), _DeadSlot()], StreamRegistry())
    with pytest.raises(MeetingAudioError):
        await fleet.speak("hello")
    assert _DeadSlot.spoken == []


def test_parse_roster_excludes_twin_self() -> None:
    from fakeyou.stream_capture import parse_roster

    payload = {
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ]
    }
    assert parse_roster(payload, twin_name="Twin") == ("devices/1", "devices/2", "devices/3")
    assert parse_roster(payload) == ("devices/1", "devices/2", "devices/3", "devices/9")


async def test_capture_drain_binds_three_remote_streams_with_self_present() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = {
        "streams": [
            {
                "slot": slot,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(3)
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ],
        "dropped": 0,
    }
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    labels = []
    for slot in range(3):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 3


async def test_stream_callback_surfaces_refusal_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.journal import Journal
    from fakeyou.record import MeetingRecord, meeting_record_path
    from fakeyou.voice_activity import SlotBinder

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, stdout=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=SlotBinder(),
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Hello from the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "unknown"
    assert rows[0]["speaker_reason"] == "no_roster"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["roster_size"] == 0


async def test_stream_callback_surfaces_bound_reason_and_roster_size(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from pipecat.frames.frames import TranscriptionFrame

    from fakeyou.cli.join import build_transcript_callback
    from fakeyou.journal import Journal
    from fakeyou.record import MeetingRecord, meeting_record_path
    from fakeyou.voice_activity import SlotBinder

    now = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, now=lambda: now, stdout=False)
    path = meeting_record_path(tmp_path, "meeting-9")
    record = MeetingRecord(path, now=lambda: now)
    binder = SlotBinder()
    binder.update_roster(["host"])
    binder.observe_stream(0, [111])
    callback = build_transcript_callback(
        journal=journal,
        record=record,
        conversation=None,
        voice_activity=None,
        binder=binder,
        per_stream_enabled=True,
    )
    frame = TranscriptionFrame(
        "Welcome to the call.", "", "2026-10-06T01:00:01+00:00", finalized=True
    )
    frame.transport_source = "slot-0"
    await callback(frame)
    record.close()
    journal.close()
    rows = [json.loads(line) for line in lines]
    rows = [row for row in rows if row["event"] == "transcript_finalized"]
    assert len(rows) == 1
    assert rows[0]["participant_id"] == "speaker-a"
    assert rows[0]["speaker_reason"] == "stream_slot"
    assert rows[0]["speaker_slot"] == 0
    assert rows[0]["speaker_bound"] is True
    assert rows[0]["roster_size"] == 1


async def test_capture_drain_loop_emits_roster_diagnostics() -> None:
    from fakeyou.journal import Journal
    from fakeyou.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [6666], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
            {"id": "devices/3", "name": "Visitor"},
            {"id": "devices/9", "name": "Twin"},
        ],
        "roster_tiles": 6,
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, stdout=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry, journal=journal, heartbeat_polls=1, twin_name="Twin")
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["roster_tiles"] == 6
    assert event["roster_pre_filter"] == 4
    assert event["roster_post_filter"] == 3
    blob = json.dumps(event)
    assert "Host" not in blob
    assert "Guest" not in blob
    assert "Visitor" not in blob
    assert "devices/1" not in blob


async def test_capture_drain_loop_roster_diagnostics_fall_back_without_tiles() -> None:
    from fakeyou.journal import Journal
    from fakeyou.stream_capture import CaptureDrainLoop

    steady = {
        "streams": [
            {"slot": 0, "csrcs": [6666], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
        ],
        "dropped": 0,
    }
    broken = {
        "streams": [{"slot": 0, "csrcs": [], "level": 0.0, "page_ms": 1000.0, "pcm_b64": "AAA="}],
        "roster": "broken",
        "roster_tiles": "nope",
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, stdout=False)
    page = meet_page([], evaluates=[steady, broken])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry, journal=journal, heartbeat_polls=1)
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["roster_tiles"] == 2
    assert first["roster_pre_filter"] == 2
    assert first["roster_post_filter"] == 2
    second = json.loads(lines[1])
    assert second["roster_tiles"] == 0
    assert second["roster_pre_filter"] == 0
    assert second["roster_post_filter"] == 0


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
    assert csrcs[0] and csrcs[1]
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


@pytest.mark.browser
async def test_stream_capture_drain_reports_roster_tile_count() -> None:
    script = load_capture_script()
    async with open_direct_page(headless=True) as page:
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate(
            "() => {"
            " document.body.innerHTML = \"<div data-participant-id='a'>\""
            " + \"<span class='notranslate'>A</span></div>\""
            " + \"<div data-participant-id=''></div>\""
            " + \"<div data-participant-id='b'>\""
            " + \"<span class='notranslate'>B</span></div>\";"
            "}"
        )
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    assert drained["roster_tiles"] == 3
    assert len(drained["roster"]) == 2


def test_parse_roster_keeps_all_entries_when_twin_name_collides() -> None:
    from fakeyou.stream_capture import parse_roster

    payload = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
    }
    assert parse_roster(payload, twin_name="Twin") == tuple(
        f"devices/{index}" for index in range(5)
    )
    mixed = {
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(4)]
        + [{"id": "devices/9", "name": "Host"}],
    }
    assert parse_roster(mixed, twin_name="Twin") == (
        tuple(f"devices/{index}" for index in range(4)) + ("devices/9",)
    )


def _collision_payload(names: list[str]) -> dict[str, object]:
    return {
        "streams": [
            {
                "slot": slot,
                "csrcs": [111 + slot],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
            for slot in range(4)
        ],
        "roster": [{"id": f"devices/{index}", "name": name} for index, name in enumerate(names)],
        "dropped": 0,
    }


async def test_capture_drain_binds_four_streams_when_twin_names_collide() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 5)
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


async def test_capture_drain_leaves_colliding_self_entry_unbound() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 5
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/0"


async def test_capture_drain_binds_speakers_with_lurker_present() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = _collision_payload(["Twin"] * 4 + ["Host"])
    roster = list(payload["roster"])
    roster.append({"id": "devices/lurker", "name": "Lurker"})
    payload["roster"] = roster
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 6
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        labels.append(outcome.label)
    assert len(set(labels)) == 4
    for slot in range(4):
        repeat = binder.resolve(slot)
        assert not isinstance(repeat, str)
        assert repeat.label == labels[slot]


async def test_capture_drain_binds_four_humans_with_unique_names() -> None:
    from fakeyou.stream_capture import STREAM_FLEET_SLOTS, CaptureDrainLoop
    from fakeyou.voice_activity import SlotBinder

    payload = _collision_payload(["Host", "Guest", "Visitor", "Partner", "Twin"])
    page = meet_page([], evaluates=[payload])
    binder = SlotBinder(capacity=STREAM_FLEET_SLOTS)
    registry, _ = _recording_registry(FakeClock().monotonic, binder)
    loop = CaptureDrainLoop(page, registry, twin_name="Twin")
    assert await loop.poll() is True
    assert binder.roster_size == 4
    labels = []
    for slot in range(4):
        outcome = binder.resolve(slot)
        assert not isinstance(outcome, str)
        assert outcome.device != "devices/4"
        labels.append(outcome.label)
    assert len(set(labels)) == 4


async def test_capture_drain_loop_reports_collision_roster_diagnostics() -> None:
    from fakeyou.journal import Journal
    from fakeyou.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {"slot": 0, "csrcs": [6666], "level": 0.4, "page_ms": 1000.0, "pcm_b64": "AAP//w=="}
        ],
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
        "roster_tiles": 5,
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, stdout=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = _recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(page, registry, journal=journal, heartbeat_polls=1, twin_name="Twin")
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["roster_tiles"] == 5
    assert event["roster_pre_filter"] == 5
    assert event["roster_post_filter"] == 5
    blob = json.dumps(event)
    assert "Twin" not in blob
    assert "devices/0" not in blob
