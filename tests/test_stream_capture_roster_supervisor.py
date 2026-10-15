import asyncio
import json
from typing import Any

import pytest

from fakeyou.audio.stream_capture import (
    DrainPolling,
    load_capture_script,
)
from fakeyou.meet.browser import BrowserLaunch, open_direct_page
from fakeyou.observability.faults import ConsumerSink
from fakeyou.observability.journal import Journal
from tests._capture_support import recording_registry
from tests._fakes_common import STREAM_CAPTURE_TEST_FLAG, FakeClock
from tests._meet_browser_fakes import meet_page


@pytest.mark.browser
async def test_stream_capture_supervisor_rescues_stalled_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        rescued = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.8)
        recovered = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in rescued["health"]}
    assert health[0]["rescued"] == 1
    assert health[1]["rescued"] == 0
    assert health[0]["stalled"] is True
    assert health[1]["stalled"] is False
    assert {entry["slot"] for entry in recovered["streams"]} == {0, 1}
    assert all(entry["pcm_b64"] for entry in recovered["streams"])


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_ended_track() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.endTestTrack(0)")
        await asyncio.sleep(3.5)
        drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.8)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert 0 in drained["released"]
    assert health.get(0, {}).get("rescued", 0) == 0
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert 1 in later_health
    assert later["released"] == []


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_stale_ended_track_without_rescue() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        drained = await page.evaluate(
            "() => { window.__fakeyouStreamCapture.endTestTrack(0);"
            " return window.__fakeyouStreamCapture.drain(); }"
        )
        await asyncio.sleep(0.8)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert drained["released"] == [0]
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert later["released"] == []


@pytest.mark.browser
async def test_stream_capture_supervisor_releases_futile_slot() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
        await page.add_init_script(script)
        await page.goto("about:blank")
        await page.evaluate("() => window.__fakeyouStreamCapture.attachTestTones()")
        await asyncio.sleep(1.5)
        await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await page.evaluate("() => window.__fakeyouStreamCapture.poisonTestSlot(0)")
        await page.evaluate("() => window.__fakeyouStreamCapture.stallTestSlot(0)")
        await asyncio.sleep(3.5)
        first = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        first_health = {entry["slot"]: entry for entry in first["health"]}
        assert first_health[0]["stalled"] is True
        assert first_health[0]["rescued"] == 0
        assert first["released"] == []
        drained = first
        for _ in range(4):
            drained = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
        await asyncio.sleep(0.5)
        later = await page.evaluate("() => window.__fakeyouStreamCapture.drain()")
    health = {entry["slot"]: entry for entry in drained["health"]}
    assert 0 not in health
    assert 1 in health
    assert drained["released"] == [0]
    later_health = {entry["slot"]: entry for entry in later["health"]}
    assert 0 not in later_health
    assert later["released"] == []


def test_parse_health_skips_malformed_entries() -> None:
    from fakeyou.audio.stream_capture import TapHealth, parse_health

    payload = {
        "health": [
            {"slot": 0, "stalled": False, "rescued": 2, "decode_fresh": True, "engine": "decode"},
            {"slot": "x", "stalled": False, "rescued": 0, "decode_fresh": False, "engine": "mstp"},
            {"slot": 1},
            "junk",
        ]
    }
    assert parse_health(payload) == {
        0: TapHealth(slot=0, stalled=False, rescued=2, decode_fresh=True, engine="decode")
    }
    assert parse_health({}) == {}


async def test_drain_loop_journals_slot_rescue_once() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    def payload(rescued: int) -> dict[str, Any]:
        return {
            "streams": [],
            "roster": [],
            "dropped": 0,
            "health": [
                {
                    "slot": 0,
                    "stalled": True,
                    "rescued": rescued,
                    "decode_fresh": False,
                    "engine": "mstp",
                }
            ],
        }

    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload(1), payload(1), payload(2)])
    loop = CaptureDrainLoop(
        page, registry, sinks=ConsumerSink(journal=journal), polling=DrainPolling(heartbeat_polls=1)
    )
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert await loop.poll() is True
    rescues = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_rescued"]
    assert [(row["slot"], row["rescued"]) for row in rescues] == [(0, 1), (0, 2)]


async def test_drain_loop_journals_released_slots_as_dropped() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [],
        "roster": [],
        "dropped": 0,
        "released": [0, 2],
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload])
    loop = CaptureDrainLoop(page, registry, sinks=ConsumerSink(journal=journal))
    assert await loop.poll() is True
    drops = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_dropped"]
    assert [(row["source"], row["slot"]) for row in drops] == [("transport", 0), ("transport", 2)]


async def test_drain_loop_ignores_malformed_released() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    def payload(released: object) -> dict[str, Any]:
        return {"streams": [], "roster": [], "dropped": 0, "released": released}

    cases = [
        "nope",
        {"slot": 0},
        None,
        [0, "1", True, -1, 1.5, None, {"slot": 2}, 3],
    ]
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    registry, _ = recording_registry(FakeClock().monotonic)
    page = meet_page([], evaluates=[payload(case) for case in cases])
    loop = CaptureDrainLoop(page, registry, sinks=ConsumerSink(journal=journal))
    for _ in cases:
        assert await loop.poll() is True
    drops = [row for line in lines if (row := json.loads(line))["event"] == "tap_slot_dropped"]
    assert [row["slot"] for row in drops] == [0, 3]
