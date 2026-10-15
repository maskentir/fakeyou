import json
from pathlib import Path

import pytest

from fakeyou.audio.audio import CaptureObserver
from fakeyou.audio.deaf import CaptureActivity
from fakeyou.foundation.attribution_probe import (
    PROBE_DRAIN_JS,
    AttributionProbe,
    ProbeDrainLoop,
    append_probe_batch,
    load_probe_script,
)
from tests._fakes_common import FakeClock
from tests._meet_browser_fakes import HangingPage, meet_page


def probe_path_text(tmp_path: Path) -> str:
    return (tmp_path / "probe.jsonl").read_text(encoding="utf-8")


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
    probe.observe_vad(active=True)
    probe.observe_pcm(32000)
    probe.observe_vad(active=False)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert [row["kind"] for row in rows] == ["vad", "vad"]
    assert rows[0]["active"] is True
    assert rows[0]["audio_ms"] == 1000.0
    assert rows[0]["mono_ms"] == 101000.0
    assert rows[1]["audio_ms"] == 3000.0


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
    assert (loop.polls, loop.poll_failures, loop.last_poll_error) == (2, 1, None)
    assert page.evaluate_calls == [PROBE_DRAIN_JS, PROBE_DRAIN_JS]
    rows = [json.loads(line) for line in probe_path_text(tmp_path).splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["mono_ms"] == 7000.0
    assert rows[0]["peer_connections"] == 1


async def test_probe_drain_loop_bounds_a_hung_drain_evaluate(tmp_path: Path) -> None:
    page = HangingPage()
    probe = AttributionProbe(
        tmp_path / "probe.jsonl",
        monotonic=FakeClock(now=7.0).monotonic,
        wall_clock=lambda: "2026-10-05T00:00:00+00:00",
    )
    loop = ProbeDrainLoop(page, probe, poll_interval_seconds=0.01)

    assert await loop.poll() is False

    assert page.cancelled is True
    assert (loop.poll_failures, loop.last_poll_error) == (1, "TimeoutError")


async def test_capture_observer_reports_pcm_and_vad_to_probe(tmp_path: Path) -> None:
    from pipecat.frames.frames import (
        InputAudioRawFrame,
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
    observer = CaptureObserver(CaptureActivity(now=clock.monotonic), probe=probe)
    await observer.process_frame(
        InputAudioRawFrame(audio=b"\x00" * 640, sample_rate=16000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await observer.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await observer.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    rows = [json.loads(line) for line in probe_path_text(tmp_path).splitlines() if line]
    assert [row["kind"] for row in rows] == ["vad", "vad"]
    assert rows[0]["audio_ms"] == 20.0
    assert rows[0]["active"] is True


async def test_capture_observer_survives_probe_io_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipecat.frames.frames import VADUserStartedSpeakingFrame
    from pipecat.processors.frame_processor import FrameDirection

    probe = AttributionProbe(tmp_path / "probe.jsonl")

    def failing(*, active: bool) -> None:
        del active
        raise OSError("disk full")

    monkeypatch.setattr(probe, "observe_vad", failing)
    observer = CaptureObserver(CaptureActivity(), probe=probe)
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
    assert (loop.polls, loop.poll_failures, loop.last_poll_error) == (1, 1, "OSError")


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


async def test_probe_drain_loop_counts_playwright_failures(tmp_path: Path) -> None:
    from playwright.async_api import Error as PlaywrightError

    page = meet_page([], evaluates=[PlaywrightError("target closed")])
    probe = AttributionProbe(tmp_path / "probe.jsonl")
    loop = ProbeDrainLoop(page, probe)

    assert await loop.poll() is False
    assert (loop.polls, loop.poll_failures, loop.last_poll_error) == (1, 1, "Error")


async def test_probe_drain_loop_reraises_unexpected_errors(tmp_path: Path) -> None:
    page = meet_page([], evaluates=[KeyError("bug")])
    probe = AttributionProbe(tmp_path / "probe.jsonl")
    loop = ProbeDrainLoop(page, probe)

    with pytest.raises(KeyError, match="bug"):
        await loop.poll()
