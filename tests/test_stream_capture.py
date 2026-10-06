import asyncio
import base64
import struct

import pytest

from fakeyou.browser import open_direct_page
from fakeyou.stream_capture import load_capture_script

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
