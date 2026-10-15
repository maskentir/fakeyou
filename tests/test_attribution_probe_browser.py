import asyncio
from typing import Any, cast

import pytest

from fakeyou.foundation.attribution_probe import (
    load_probe_script,
)
from fakeyou.meet.browser import BrowserLaunch, open_direct_page


async def wait_for_pending(page: object, key: str, timeout_seconds: float = 15.0) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        pending = await page.evaluate("() => window.__fakeyouAttributionProbe.pending()")
        if pending["peer_connections"] == 2 or key == "roster":
            if pending[key]:
                return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"probe never recorded {key}: {pending!r}")
        await asyncio.sleep(0.1)


async def wait_for_levels(page: object, timeout_seconds: float = 15.0) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        pending = await page.evaluate("() => window.__fakeyouAttributionProbe.pending()")
        if pending["levels"]:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"probe never recorded levels: {pending!r}")
        await asyncio.sleep(0.1)


async def wait_for_audio_topology(page: object, timeout_seconds: float = 15.0) -> None:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        pending = await page.evaluate("() => window.__fakeyouAttributionProbe.pending()")
        if pending["census"] and pending["topology"]:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"probe never recorded topology: {pending!r}")
        await asyncio.sleep(0.1)


async def wait_for_loud_levels(page: object, timeout_seconds: float = 15.0) -> dict[str, Any]:
    from playwright.async_api import Page

    assert isinstance(page, Page)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        drained = await page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        if any(entry["audioLevel"] > 0.1 for entry in drained["levels"]):
            return cast(dict[str, Any], drained)
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("probe never recorded loud levels")
        await asyncio.sleep(0.2)


LOOPBACK_ENCODED_RECEIVER = (
    "async () => {"
    " const ctx = new AudioContext(); await ctx.resume();"
    " const tone = ctx.createOscillator(); const out = ctx.createMediaStreamDestination();"
    " tone.connect(out); tone.start();"
    " const a = new RTCPeerConnection();"
    " const b = new RTCPeerConnection({encodedInsertableStreams: true});"
    " a.onicecandidate = (e) => e.candidate && b.addIceCandidate(e.candidate);"
    " b.onicecandidate = (e) => e.candidate && a.addIceCandidate(e.candidate);"
    " b.ontrack = (e) => {"
    "  const streams = e.receiver.createEncodedStreams();"
    "  streams.readable.pipeTo(streams.writable);"
    "  ctx.createMediaStreamSource(e.streams[0]).connect(ctx.destination); };"
    " a.addTrack(out.stream.getAudioTracks()[0], out.stream);"
    " await a.setLocalDescription(await a.createOffer());"
    " await b.setRemoteDescription(a.localDescription);"
    " await b.setLocalDescription(await b.createAnswer());"
    " await a.setRemoteDescription(b.localDescription);"
    "}"
)


@pytest.mark.browser
async def test_probe_captures_peer_connections_and_samples_sources() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/call",
            lambda route: route.fulfill(
                body="<button>call</button><script>"
                "new RTCPeerConnection(); new RTCPeerConnection();"
                "</script>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/call", wait_until="load")
        await real_page.evaluate(
            "() => { RTCPeerConnection.prototype.getReceivers = () => [{"
            " track: {kind: 'audio', id: 'track-abc', muted: false, readyState: 'live'},"
            " getContributingSources: () => [{source: 11, audioLevel: 0.5, timestamp: 7}],"
            " getSynchronizationSources: () => [{source: 11, audioLevel: 0.5, timestamp: 7}],"
            " getStats: async () => new Map([['r1', {type: 'inbound-rtp', kind: 'audio',"
            " trackIdentifier: 'track-abc', audioLevel: 0.7}]]),"
            "}, {"
            " track: {kind: 'audio', id: 'track-def', muted: false, readyState: 'live'},"
            " getContributingSources: () => [],"
            " getSynchronizationSources: () => [],"
            " getStats: async () => ({r2: {type: 'inbound-rtp', kind: 'audio',"
            " trackIdentifier: 'track-def', audioLevel: 0.3}}),"
            "}]; }"
        )
        await wait_for_pending(real_page, "samples")
        drained = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert drained["peer_connections"] == 2
        assert drained["samples"], "probe recorded no source samples"
        levels = [
            entry["audioLevel"] for sample in drained["samples"] for entry in sample["sources"]
        ]
        assert 11 in [
            entry["source"] for sample in drained["samples"] for entry in sample["sources"]
        ]
        assert all(level == 0.5 for level in levels)
        stamps = [sample["page_ms"] for sample in drained["samples"]]
        assert stamps == sorted(stamps)
        assert drained["samples"][0]["track"]["id"] == "track-abc"
        await wait_for_levels(real_page)
        leveled = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert leveled["levels"], "probe recorded no per-track levels"
        first = {entry["source"]: entry["audioLevel"] for entry in leveled["levels"]}
        assert sorted(first.values()) == [0.3, 0.7]
        assert all(isinstance(source, int) for source in first)
        assert leveled["receiver_count"] == 2
        await wait_for_levels(real_page)
        releveled = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        second = {entry["source"]: entry["audioLevel"] for entry in releveled["levels"]}
        assert second == first
        diag = {entry["track"]: entry for entry in releveled["stats_diag"]}
        assert set(diag) == {"track-abc", "track-def"}
        assert diag["track-abc"]["entries"] == 1
        assert "inbound-rtp" in diag["track-abc"]["types"]
        assert diag["track-abc"]["audio"] is True
        assert diag["track-abc"]["error"] is None
        again = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert again["page_ms"] >= drained["page_ms"]
        assert all(sample["page_ms"] >= stamps[-1] for sample in again["samples"])


@pytest.mark.browser
async def test_probe_reports_roster_with_structural_self() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/roster",
            lambda route: route.fulfill(
                body="<div data-participant-id='spaces/a/devices/1'>"
                "<span class='notranslate'>Rani Twin</span>"
                "<span data-self-name='Rani Twin'></span></div>"
                "<div data-participant-id='spaces/a/devices/2'>"
                "<span class='notranslate'>Host Human</span></div>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/roster", wait_until="load")
        await wait_for_pending(real_page, "roster")
        drained = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert drained["roster"], "probe recorded no roster snapshots"
        tiles = {tile["id"]: tile for tile in drained["roster"][-1]["tiles"]}
        assert tiles["spaces/a/devices/1"]["self"] is True
        assert tiles["spaces/a/devices/1"]["name"] == "Rani Twin"
        assert tiles["spaces/a/devices/2"]["self"] is False
        assert tiles["spaces/a/devices/2"]["name"] == "Host Human"
        assert "notranslate" in tiles["spaces/a/devices/2"]["html"]
        assert tiles["spaces/a/devices/2"]["aria"] == ""
        assert tiles["spaces/a/devices/2"]["cls"] == ""


@pytest.mark.browser
async def test_probe_reports_media_element_census() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/media",
            lambda route: route.fulfill(
                body="<div data-participant-id='spaces/a/devices/1'>"
                "<span class='notranslate'>Rani</span>"
                "<audio id='a1'></audio></div>"
                "<video id='v1'></video>"
                "<script>"
                "document.getElementById('a1').srcObject = new MediaStream();"
                "</script>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/media", wait_until="domcontentloaded")
        await real_page.evaluate(
            "() => { new RTCPeerConnection().getStats = async () =>"
            " new Map([['p1', {type: 'peer-connection'}]]);"
            " window.__ctx = new AudioContext(); }"
        )
        await wait_for_audio_topology(real_page)
        drained = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        census = drained["media_census"]
        assert len(census) == 2
        audio = next(entry for entry in census if entry["tag"] == "audio")
        assert audio["hasSrcObject"] is True
        assert audio["participantId"] == "spaces/a/devices/1"
        assert audio["tileName"] == "Rani"
        topology = drained["audio_topology"]
        assert topology["contexts"] >= 1
        assert topology["pc_stats_entries"] >= 1


@pytest.mark.browser
async def test_probe_counts_entries_of_a_real_stats_report() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/stats",
            lambda route: route.fulfill(
                body="<audio></audio><script>window.__peer = new RTCPeerConnection();"
                "window.__ctx = new AudioContext();</script>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/stats", wait_until="domcontentloaded")
        await wait_for_audio_topology(real_page)
        drained = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        topology = drained["audio_topology"]
        assert topology["pc_stats_entries"] >= 1
        assert "peer-connection" in topology["pc_stats_types"]


@pytest.mark.browser
async def test_probe_reads_identity_and_level_from_encoded_audio_frames() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        try:
            page = await browser.new_page()
            await page.add_init_script(load_probe_script())
            await page.goto("about:blank")
            await page.evaluate(LOOPBACK_ENCODED_RECEIVER)
            deadline = asyncio.get_running_loop().time() + 15.0
            windows: list[dict[str, Any]] = []
            while len(windows) < 5 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.3)
                drained = await page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
                windows += drained["frames"]
            assert len(windows) >= 5
            assert drained["pipeline"]["encoded_streams"] == 1
            assert all(len(window["ssrc"]) == 1 for window in windows)
            assert max(window["level"] for window in windows) > 0.1
            assert windows[0]["track"]
        finally:
            await browser.close()


@pytest.mark.browser
async def test_probe_taps_webaudio_source_energy() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/tone",
            lambda route: route.fulfill(
                body="<button>tone</button><script>"
                "const ctx = new AudioContext();"
                "const osc = ctx.createOscillator();"
                "const dest = ctx.createMediaStreamDestination();"
                "osc.connect(dest);"
                "osc.start();"
                "ctx.createMediaStreamSource(dest.stream);"
                "ctx.resume();"
                "</script>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/tone", wait_until="load")
        drained = await wait_for_loud_levels(real_page)
        assert any(entry["audioLevel"] > 0.1 for entry in drained["levels"])
        assert drained["audio_topology"]["contexts"] >= 1
        assert len(drained["audio_topology"]["sources"]) >= 1


@pytest.mark.browser
async def test_probe_samplers_survive_hostile_dom() -> None:
    async with open_direct_page(launch=BrowserLaunch(headless=True)) as real_page:
        await real_page.add_init_script(load_probe_script())
        await real_page.route(
            "https://probe.fakeyou.test/hostile",
            lambda route: route.fulfill(
                body="<button>hostile</button>",
                content_type="text/html",
            ),
        )
        await real_page.goto("https://probe.fakeyou.test/hostile", wait_until="load")
        await real_page.evaluate(
            "() => { document.querySelectorAll = () => { throw new Error('broken'); }; }"
        )
        await asyncio.sleep(0.4)
        await real_page.evaluate("() => { delete document.querySelectorAll; }")
        await wait_for_pending(real_page, "roster")
        drained = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert drained["roster"] != []
