import asyncio
import base64
import math
import os
import re
import struct
import sys
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from playwright.async_api import Page

from fakeyou.audio import (
    AGENT_MIC_SOURCE_NAME,
    AGENT_SINK_NAME,
    MEETING_MONITOR_SOURCE_NAME,
    MEETING_SINK_NAME,
    PLAYBACK_SAMPLE_RATE,
    open_meeting_audio,
)
from fakeyou.browser import open_direct_page
from fakeyou.cli import tts_api_key
from fakeyou.playback import (
    PlaybackEngine,
    create_pactl_muter,
    create_paplay_runner,
    create_paplay_stream_runner,
)
from fakeyou.settings import Settings, require_secret
from tests.fakes import FakeSTT

PAGE = "<!doctype html><html><body><h1>Audio fixture</h1></body></html>"


class _ToneSynth:
    def _audio(self) -> bytes:
        samples = (
            round(10000 * math.sin(2 * math.pi * 440 * index / PLAYBACK_SAMPLE_RATE))
            for index in range(72_000)
        )
        return struct.pack("<72000h", *samples)

    async def synthesize(self, text: str) -> bytes:
        if not text.strip():
            raise ValueError("cannot synthesize blank text")
        return self._audio()

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            raise ValueError("cannot synthesize blank text")
        audio = self._audio()
        for offset in range(0, len(audio), 4096):
            await asyncio.sleep(0)
            yield audio[offset : offset + 4096]

    async def aclose(self) -> None:
        pass


def _container_playback(*, streaming: bool) -> PlaybackEngine:
    return PlaybackEngine(
        _ToneSynth(),
        play_audio=create_paplay_runner(sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE),
        stream_audio=(
            create_paplay_stream_runner(sink=AGENT_SINK_NAME, rate=PLAYBACK_SAMPLE_RATE)
            if streaming
            else None
        ),
        streaming=streaming,
        set_muted=create_pactl_muter(sink=AGENT_SINK_NAME),
    )


@asynccontextmanager
async def _audio_page() -> AsyncIterator[Page]:
    async with open_direct_page(
        headless=False,
        environment={"PULSE_SOURCE": AGENT_MIC_SOURCE_NAME, "PULSE_SINK": MEETING_SINK_NAME},
    ) as page:
        await page.route(
            "https://audio.fakeyou.test/**",
            lambda route: route.fulfill(body=PAGE, content_type="text/html"),
        )
        await page.goto("https://audio.fakeyou.test/", wait_until="domcontentloaded")
        await page.evaluate(
            """async () => {
              const context = new AudioContext();
              const stream = await navigator.mediaDevices.getUserMedia({
                audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false},
                video: false
              });
              const analyser = context.createAnalyser();
              analyser.fftSize = 2048;
              context.createMediaStreamSource(stream).connect(analyser);
              const oscillator = context.createOscillator();
              const gain = context.createGain();
              oscillator.frequency.value = 660;
              gain.gain.value = 0.3;
              oscillator.connect(gain).connect(context.destination);
              await context.resume();
              window.audioProbe = {
                context, stream, analyser, oscillator,
                start: () => oscillator.start(),
                stop: () => oscillator.stop(),
                async sample(milliseconds) {
                  const data = new Float32Array(analyser.fftSize);
                  const levels = [];
                  const end = performance.now() + milliseconds;
                  while (performance.now() < end) {
                    analyser.getFloatTimeDomainData(data);
                    levels.push(Math.sqrt(data.reduce((sum, x) => sum + x * x, 0) / data.length));
                    await new Promise(resolve => setTimeout(resolve, 50));
                  }
                  return levels;
                },
                async playWav(dataUri) {
                  const audio = new Audio(dataUri);
                  await new Promise((resolve, reject) => {
                    audio.onended = resolve;
                    audio.onerror = () => reject(new Error('WAV playback failed'));
                    audio.play().catch(reject);
                  });
                },
                async close() {
                  stream.getTracks().forEach(track => track.stop());
                  await context.close();
                }
              };
            }"""
        )
        try:
            yield page
        finally:
            await page.evaluate("window.audioProbe.close()")


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", text).casefold()).split())


@pytest.mark.browser
@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streamed"])
@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME,
    reason="requires the Linux audio worker and isolated PulseAudio source",
)
async def test_browser_audio_reaches_native_transport_and_agent_tone_reaches_browser(
    streaming: bool,
) -> None:
    stt = FakeSTT(finalized=(), signal=True)

    async def on_transcript(_frame) -> None:
        pass

    async with _audio_page() as page:
        async with open_meeting_audio(
            muse_api_key="unused",
            tts_api_key="unused",
            on_transcript=on_transcript,
            stt=stt,
            playback=_container_playback(streaming=streaming),
        ) as session:
            await page.evaluate("window.audioProbe.start()")
            await asyncio.wait_for(stt.signal.wait(), timeout=10)
            assert max(stt.levels) > 0.02

            await page.evaluate("window.audioProbe.stop()")
            await asyncio.sleep(0.7)
            stt.levels.clear()
            quiet = await page.evaluate("window.audioProbe.sample(500)")
            assert max(quiet) < 0.015

            captured = asyncio.create_task(page.evaluate("window.audioProbe.sample(3800)"))
            try:
                await asyncio.sleep(0.2)
                await session.speak("container roundtrip")
                browser_levels = await captured
            finally:
                if not captured.done():
                    captured.cancel()
                    await asyncio.gather(captured, return_exceptions=True)
            assert max(browser_levels) > 0.02
            assert max(stt.levels, default=0) < 0.015
            session.raise_if_failed()


@pytest.mark.browser
@pytest.mark.live
@pytest.mark.skipif(
    sys.platform != "linux" or os.getenv("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME,
    reason="requires the Linux audio worker and isolated PulseAudio source",
)
async def test_browser_audio_reaches_muse_and_twin_speech_reaches_browser() -> None:
    audio_name = os.getenv("FAKEYOU_MUSE_TEST_AUDIO")
    expected = os.getenv("FAKEYOU_MUSE_EXPECTED_TEXT")
    if not audio_name or not expected:
        pytest.skip("set FAKEYOU_MUSE_TEST_AUDIO and FAKEYOU_MUSE_EXPECTED_TEXT")
    settings = Settings()
    try:
        muse_key = require_secret(settings.model_api_key, "FAKEYOU_MODEL_API_KEY")
        speech_key = tts_api_key(settings)
    except ValueError as error:
        pytest.skip(str(error))
    expected_text = _normalise(expected)
    if not expected_text:
        pytest.fail("FAKEYOU_MUSE_EXPECTED_TEXT must contain text")
    wav = await asyncio.to_thread(Path(audio_name).read_bytes)
    data_uri = "data:audio/wav;base64," + base64.b64encode(wav).decode("ascii")
    finals: list[str] = []
    received = asyncio.Event()

    async def on_transcript(frame) -> None:
        finals.append(frame.text)
        if _normalise(" ".join(finals)) == expected_text:
            received.set()

    async with _audio_page() as page:
        async with open_meeting_audio(
            muse_api_key=muse_key,
            tts_api_key=speech_key,
            tts_provider=settings.tts_provider,
            tts_voice=settings.tts_voice,
            on_transcript=on_transcript,
        ) as session:
            await page.evaluate("dataUri => window.audioProbe.playWav(dataUri)", data_uri)
            await asyncio.wait_for(received.wait(), timeout=35)
            assert _normalise(" ".join(finals)) == expected_text
            await asyncio.sleep(1)
            final_count = len(finals)
            quiet = await page.evaluate("window.audioProbe.sample(500)")
            assert max(quiet) < 0.015
            captured = asyncio.create_task(page.evaluate("window.audioProbe.sample(12000)"))
            try:
                await asyncio.sleep(0.2)
                await session.speak("I heard you clearly.")
                browser_levels = await captured
            finally:
                if not captured.done():
                    captured.cancel()
                    await asyncio.gather(captured, return_exceptions=True)
            assert max(browser_levels) > 0.02
            assert len(finals) == final_count
            session.raise_if_failed()
