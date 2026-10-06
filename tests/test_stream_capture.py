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
