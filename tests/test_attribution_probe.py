import asyncio
import json
from pathlib import Path

import pytest

from fakeyou.attribution_probe import (
    PROBE_DRAIN_JS,
    AttributionProbe,
    ProbeDrainLoop,
    append_probe_batch,
    load_probe_script,
)
from fakeyou.audio import _CaptureObserver
from fakeyou.browser import open_direct_page
from fakeyou.deaf import CaptureActivity
from fakeyou.meet import GuestJoinRequest, request_guest_join
from fakeyou.probe_replay import (
    _percentile,
    _vad_windows,
    align_turns,
    csrc_lag_ms,
    page_to_mono,
    reference_precision,
    source_sets_by_speaker,
    summarize_probe_session,
)
from fakeyou.runtime import run_meeting_session
from fakeyou.session import SessionConfig
from fakeyou.settings import DEFAULTS
from tests.fakes import (
    IN_CALL,
    LINK,
    FakeClock,
    FakePageSession,
    FakeSaverSession,
    meet_page,
)


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


def test_probe_script_wraps_peer_connection() -> None:
    script = load_probe_script()
    assert "RTCPeerConnection" in script
    assert "__fakeyouAttributionProbe" in script
    assert "getContributingSources" in script
    assert "getSynchronizationSources" in script


def test_append_probe_batch_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    clock = FakeClock(now=12.0)
    append_probe_batch(
        path,
        {"page_ms": 100.0, "samples": [], "roster": [], "peer_connections": 0},
        mono_ms=clock.now * 1000.0,
        wall="2026-10-05T00:00:00+00:00",
    )
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["kind"] == "page"
    assert row["mono_ms"] == 12000.0
    assert row["peer_connections"] == 0


def test_probe_audio_clock_stamps_vad_edges(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    clock = FakeClock(now=100.0)
    probe = AttributionProbe(
        path,
        monotonic=clock.monotonic,
        wall_clock=lambda: "2026-10-05T00:00:00+00:00",
        sample_rate=16000,
    )
    probe.observe_pcm(16000)
    clock.now = 101.0
    probe.observe_vad(True)
    probe.observe_pcm(32000)
    probe.observe_vad(False)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [row["kind"] for row in rows] == ["vad", "vad"]
    assert rows[0]["active"] is True
    assert rows[0]["audio_ms"] == 1000.0
    assert rows[0]["mono_ms"] == 101000.0
    assert rows[1]["audio_ms"] == 3000.0


@pytest.mark.browser
async def test_probe_captures_peer_connections_and_samples_sources() -> None:
    async with open_direct_page(headless=True) as real_page:
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
            " track: {kind: 'audio'},"
            " getContributingSources: () => [{source: 11, audioLevel: 0.5, timestamp: 7}],"
            " getSynchronizationSources: () => [{source: 11, audioLevel: 0.5, timestamp: 7}],"
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
        again = await real_page.evaluate("() => window.__fakeyouAttributionProbe.drain()")
        assert again["page_ms"] >= drained["page_ms"]
        assert all(sample["page_ms"] >= stamps[-1] for sample in again["samples"])


@pytest.mark.browser
async def test_probe_reports_roster_with_structural_self() -> None:
    async with open_direct_page(headless=True) as real_page:
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


async def test_guest_join_installs_init_scripts_before_navigation() -> None:
    page = meet_page([IN_CALL])
    clock = FakeClock()
    request = GuestJoinRequest(link=LINK, twin_name="Twin")
    result = await request_guest_join(
        page,
        request,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        init_scripts=("probe();",),
    )
    assert result.status == "admitted"
    assert page.init_scripts == ["probe();"]
    assert page.visited == [f"{LINK}?hl=en"]


def test_probe_wiring_defaults_off() -> None:
    assert DEFAULTS.conversation.attribution_probe_enabled is False
    config = SessionConfig(owner_id="o", twin_id="t", meeting_id="m", link=LINK, twin_name="Twin")
    assert config.attribution_probe_path is None


async def test_run_meeting_session_installs_probe_script_when_enabled(
    tmp_path: Path,
) -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    config = SessionConfig(
        owner_id="o",
        twin_id="t",
        meeting_id="m",
        link=LINK,
        twin_name="Twin",
        attribution_probe_path=tmp_path / "probe.jsonl",
    )
    result = await run_meeting_session(
        config,
        stop=stop,
        open_page=browser.open,
        saver_factory=FakeSaverSession().factory,
        sleep=FakeClock().sleep,
        monotonic=FakeClock().monotonic,
    )
    assert result.phase == "ended"
    assert page.init_scripts == [load_probe_script()]


async def test_run_meeting_session_skips_probe_script_when_disabled() -> None:
    stop = asyncio.Event()
    page = meet_page(
        [IN_CALL, IN_CALL, IN_CALL],
        on_content=lambda reads: stop.set() if reads >= 2 else None,
    )
    browser = FakePageSession(page)
    config = SessionConfig(owner_id="o", twin_id="t", meeting_id="m", link=LINK, twin_name="Twin")
    await run_meeting_session(
        config,
        stop=stop,
        open_page=browser.open,
        saver_factory=FakeSaverSession().factory,
        sleep=FakeClock().sleep,
        monotonic=FakeClock().monotonic,
    )
    assert page.init_scripts == []


async def test_probe_drain_loop_appends_batches(tmp_path: Path) -> None:
    batch = {"page_ms": 50.0, "samples": [], "roster": [], "peer_connections": 1}
    page = meet_page([], evaluates=[batch, None])
    clock = FakeClock(now=7.0)
    probe = AttributionProbe(
        tmp_path / "probe.jsonl",
        monotonic=clock.monotonic,
        wall_clock=lambda: "2026-10-05T00:00:00+00:00",
    )
    loop = ProbeDrainLoop(page, probe)
    assert await loop.poll() is True
    assert await loop.poll() is False
    assert page.evaluate_calls == [PROBE_DRAIN_JS, PROBE_DRAIN_JS]
    rows = [json.loads(line) for line in probe_path_text(tmp_path).splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["mono_ms"] == 7000.0
    assert rows[0]["peer_connections"] == 1


def probe_path_text(tmp_path: Path) -> str:
    return (tmp_path / "probe.jsonl").read_text(encoding="utf-8")


async def test_capture_observer_reports_pcm_and_vad_to_probe(tmp_path: Path) -> None:
    from pipecat.frames.frames import (
        AudioRawFrame,
        VADUserStartedSpeakingFrame,
        VADUserStoppedSpeakingFrame,
    )
    from pipecat.processors.frame_processor import FrameDirection

    clock = FakeClock(now=20.0)
    probe = AttributionProbe(
        tmp_path / "probe.jsonl",
        monotonic=clock.monotonic,
        wall_clock=lambda: "2026-10-05T00:00:00+00:00",
    )
    observer = _CaptureObserver(CaptureActivity(now=clock.monotonic), probe=probe)
    await observer.process_frame(
        AudioRawFrame(audio=b"\x00" * 640, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    rows = [json.loads(line) for line in probe_path_text(tmp_path).splitlines() if line]
    assert [row["kind"] for row in rows] == ["vad", "vad"]
    assert rows[0]["audio_ms"] == 20.0
    assert rows[0]["active"] is True


def test_align_turns_matches_fragments_to_transcripts() -> None:
    transcripts = [
        {"text": "good morning everyone", "wall": "2026-10-05T10:00:01+00:00"},
        {"text": "orchid demo next week", "wall": "2026-10-05T10:00:05+00:00"},
    ]
    turns = [
        {"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "GOOD MORNING"},
        {"turn": 2, "speaker": "Budi", "kind": "solo", "fragment": "orchid"},
    ]
    aligned = align_turns(transcripts, turns)
    assert [(entry["turn"], entry["speaker"]) for entry in aligned] == [
        (1, "Rani"),
        (2, "Budi"),
    ]


def test_csrc_lag_measures_first_active_sample_after_vad_start() -> None:
    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.0}]},
        {"page_ms": 1050.0, "sources": [{"source": 3, "audioLevel": 0.4}]},
        {"page_ms": 1100.0, "sources": [{"source": 3, "audioLevel": 0.5}]},
    ]
    anchors = [(1000.0, 5000.0)]
    lag = csrc_lag_ms(samples, anchors, vad_start_mono_ms=5025.0, threshold=0.01)
    assert lag == 25.0


def test_source_sets_and_reference_precision_from_replay() -> None:
    samples = [
        {"page_ms": 1000.0, "sources": [{"source": 3, "audioLevel": 0.4}]},
        {"page_ms": 2000.0, "sources": [{"source": 9, "audioLevel": 0.6}]},
    ]
    anchors = [(1000.0, 5000.0)]
    windows = [
        {"speaker": "Rani", "kind": "solo", "start_mono_ms": 4900.0, "end_mono_ms": 5100.0},
        {"speaker": "Budi", "kind": "solo", "start_mono_ms": 5900.0, "end_mono_ms": 6100.0},
    ]
    assert source_sets_by_speaker(samples, anchors, windows) == {
        "Rani": (3,),
        "Budi": (9,),
    }
    report = reference_precision(samples, anchors, windows)
    assert report["precision"] == 1.0
    assert report["overlap_unknown_rate"] is None


def test_summarize_probe_session_reports_gate_metrics(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "vad",
                        "active": True,
                        "audio_ms": 100.0,
                        "mono_ms": 7400.0,
                        "wall": "2026-10-05T10:00:00+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "vad",
                        "active": False,
                        "audio_ms": 1100.0,
                        "mono_ms": 7700.0,
                        "wall": "2026-10-05T10:00:01+00:00",
                    }
                ),
                json.dumps(
                    {
                        "kind": "page",
                        "mono_ms": 8000.0,
                        "wall": "2026-10-05T10:00:03+00:00",
                        "page_ms": 2000.0,
                        "peer_connections": 1,
                        "samples": [
                            {
                                "page_ms": 1500.0,
                                "sources": [{"source": 3, "audioLevel": 0.4}],
                            },
                            {
                                "page_ms": 1600.0,
                                "sources": [{"source": 3, "audioLevel": 0.5}],
                            },
                        ],
                        "roster": [
                            {
                                "page_ms": 1500.0,
                                "tiles": [
                                    {
                                        "id": "a",
                                        "name": "Rani",
                                        "self": False,
                                        "speaking": {
                                            "es310d": True,
                                            "knownClass": False,
                                        },
                                        "muted": False,
                                    },
                                    {
                                        "id": "b",
                                        "name": "Rani Twin",
                                        "self": True,
                                        "speaking": {
                                            "es310d": False,
                                            "knownClass": False,
                                        },
                                        "muted": True,
                                    },
                                ],
                            }
                        ],
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    record_path = tmp_path / "record.jsonl"
    record_path.write_text(
        json.dumps(
            {
                "kind": "transcript",
                "at": "2026-10-05T10:00:02+00:00",
                "id": "t1",
                "spoken_at": "2026-10-05T10:00:02+00:00",
                "source": "meeting_audio",
                "text": "good morning all",
                "participant_id": "unknown",
                "speaker_evidence_id": None,
            }
        ),
        encoding="utf-8",
    )
    truth_path = tmp_path / "truth.json"
    truth_path.write_text(
        json.dumps([{"turn": 1, "speaker": "Rani", "kind": "solo", "fragment": "morning"}]),
        encoding="utf-8",
    )
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["turns"] == 1
    assert report["aligned"] == 1
    assert report["lag_ms"]["median"] == 100.0
    assert report["source_sets"] == {"Rani": [3]}
    assert report["reference"]["precision"] == 1.0
    assert report["markers"]["es310d"]["accuracy"] == 1.0
    assert report["mute"]["keeps_speaker"] == 1.0
    assert report["gates"]["name_precision"] is True


def test_append_probe_batch_ignores_page_controlled_clocks(tmp_path: Path) -> None:
    path = tmp_path / "probe.jsonl"
    append_probe_batch(
        path,
        {"kind": "vad", "mono_ms": 999.0, "wall": "forged", "samples": []},
        mono_ms=12000.0,
        wall="2026-10-05T00:00:00+00:00",
    )
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["kind"] == "page"
    assert row["mono_ms"] == 12000.0
    assert row["wall"] == "2026-10-05T00:00:00+00:00"


async def test_capture_observer_survives_probe_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    probe = AttributionProbe(tmp_path / "probe.jsonl")

    def failing(_active: bool) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(probe, "observe_vad", failing)
    observer = _CaptureObserver(CaptureActivity(), probe=probe)
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)


async def test_probe_drain_loop_survives_append_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = meet_page([], evaluates=[{"page_ms": 1.0}])
    probe = AttributionProbe(tmp_path / "probe.jsonl")

    def failing(_batch: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(probe, "append_page_batch", failing)
    loop = ProbeDrainLoop(page, probe)
    assert await loop.poll() is False


async def test_poll_stamps_mono_at_drain_midpoint(tmp_path: Path) -> None:
    page = meet_page([], evaluates=[{"page_ms": 1.0, "samples": []}])
    stamps = iter([7.0, 7.2])
    probe = AttributionProbe(
        tmp_path / "probe.jsonl",
        monotonic=lambda: next(stamps),
        wall_clock=lambda: "2026-10-05T00:00:00+00:00",
    )
    loop = ProbeDrainLoop(page, probe)
    assert await loop.poll() is True
    row = json.loads((tmp_path / "probe.jsonl").read_text(encoding="utf-8").strip())
    assert row["mono_ms"] == 7100.0


def test_vad_windows_follow_mono_order_and_drop_inverted() -> None:
    edges = [
        {
            "active": True,
            "mono_ms": 1000.0,
            "wall": "2026-10-05T10:00:02+00:00",
        },
        {
            "active": False,
            "mono_ms": 3000.0,
            "wall": "2026-10-05T10:00:00+00:00",
        },
        {
            "active": True,
            "mono_ms": 4000.0,
            "wall": "2026-10-05T10:00:04+00:00",
        },
        {
            "active": False,
            "mono_ms": 4000.0,
            "wall": "2026-10-05T10:00:05+00:00",
        },
    ]
    windows = _vad_windows(edges)
    assert [(window["start_mono_ms"], window["end_mono_ms"]) for window in windows] == [
        (1000.0, 3000.0)
    ]


def test_summarize_skips_corrupt_lines_with_counts(tmp_path: Path) -> None:
    probe_path = tmp_path / "probe.jsonl"
    probe_path.write_text("{bad json\n", encoding="utf-8")
    record_path = tmp_path / "record.jsonl"
    record_path.write_text("not json either\n", encoding="utf-8")
    truth_path = tmp_path / "truth.json"
    truth_path.write_text("[]", encoding="utf-8")
    report = summarize_probe_session(probe_path, record_path, truth_path)
    assert report["turns"] == 0
    assert report["skipped_lines"] == {"probe": 1, "record": 1}


def test_page_to_mono_rejects_empty_anchors() -> None:
    with pytest.raises(ValueError):
        page_to_mono(1000.0, [])


def test_percentile_uses_nearest_rank() -> None:
    assert _percentile(list(range(100)), 0.95) == 94


@pytest.mark.browser
async def test_probe_samplers_survive_hostile_dom() -> None:
    async with open_direct_page(headless=True) as real_page:
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
