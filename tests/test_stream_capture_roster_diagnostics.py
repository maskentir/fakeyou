import json

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


async def test_capture_drain_loop_emits_level_heartbeat() -> None:
    import json

    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [111],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
                "track": "abc123",
                "engine": "decode",
            },
            {
                "slot": 1,
                "seq": 0,
                "csrcs": [],
                "level": 0.0,
                "page_ms": 1000.0,
                "pcm_b64": "AAA=",
                "track": "def456",
                "engine": "mstp",
            },
        ],
        "roster": [],
        "dropped": 2,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload, payload, payload])
    registry, _ = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, sinks=ConsumerSink(journal=journal), polling=DrainPolling(heartbeat_polls=3)
    )
    assert await loop.poll() is True
    assert await loop.poll() is True
    assert lines == []
    assert await loop.poll() is True
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["event"] == "capture_polled"
    assert event["dropped"] == 2
    assert event["fails"] == 0
    assert event["slots"]["0"] == {
        "level": 0.4,
        "bytes": 4,
        "track": "abc123",
        "engine": "decode",
        "stalled": False,
        "rescued": 0,
        "decode_fresh": False,
    }
    assert event["slots"]["1"] == {
        "level": 0.0,
        "bytes": 2,
        "track": "def456",
        "engine": "mstp",
        "stalled": False,
        "rescued": 0,
        "decode_fresh": False,
    }


async def test_capture_drain_loop_emits_roster_diagnostics() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
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
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page,
        registry,
        sinks=ConsumerSink(journal=journal),
        polling=DrainPolling(heartbeat_polls=1),
        twin_name="Twin",
    )
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
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    steady = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [
            {"id": "devices/1", "name": "Host"},
            {"id": "devices/2", "name": "Guest"},
        ],
        "dropped": 0,
    }
    broken = {
        "streams": [
            {"slot": 0, "seq": 0, "csrcs": [], "level": 0.0, "page_ms": 1000.0, "pcm_b64": "AAA="}
        ],
        "roster": "broken",
        "roster_tiles": "nope",
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[steady, broken])
    registry, _ = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page, registry, sinks=ConsumerSink(journal=journal), polling=DrainPolling(heartbeat_polls=1)
    )
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


@pytest.mark.browser
async def test_stream_capture_drain_reports_roster_tile_count() -> None:
    script = load_capture_script()
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as page:
        await page.add_init_script(STREAM_CAPTURE_TEST_FLAG)
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


async def test_capture_drain_loop_reports_collision_roster_diagnostics() -> None:
    from fakeyou.audio.stream_capture import CaptureDrainLoop

    payload = {
        "streams": [
            {
                "slot": 0,
                "seq": 0,
                "csrcs": [6666],
                "level": 0.4,
                "page_ms": 1000.0,
                "pcm_b64": "AAP//w==",
            }
        ],
        "roster": [{"id": f"devices/{index}", "name": "Twin"} for index in range(5)],
        "roster_tiles": 5,
        "dropped": 0,
    }
    lines: list[str] = []
    journal = Journal("meeting-9", emit=lines.append, echo=False)
    page = meet_page([], evaluates=[payload])
    registry, _ = recording_registry(FakeClock().monotonic)
    loop = CaptureDrainLoop(
        page,
        registry,
        sinks=ConsumerSink(journal=journal),
        polling=DrainPolling(heartbeat_polls=1),
        twin_name="Twin",
    )
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
