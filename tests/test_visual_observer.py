from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from playwright.async_api import Error as PlaywrightError

from fakeyou.config.settings import VisualSettings
from fakeyou.foundation.polling import PollSchedule
from fakeyou.observability.faults import ConsumerSink
from fakeyou.visual.visual_caption import VisualCaptioner
from fakeyou.visual.visual_observer import (
    VISUAL_CONTENT_BOX_JS,
    VISUAL_MIN_CLIP_HEIGHT,
    VISUAL_MIN_CLIP_WIDTH,
    VISUAL_SCAN_JS,
    CaptionSink,
    VisualObserver,
)
from fakeyou.visual.visual_store import VisualStore, dhash_image, read_visual_sidecar
from tests._meet_browser_fakes import HangingPage
from tests._persistence_fakes import RecordingJournal
from tests._visual_fakes import CLIP_BOX, PRESENT_SCAN, FakeShotPage, caption_model, shot_bytes

ABSENT_SCAN: dict[str, Any] = {
    "labels": ["Alex", "Leave call"],
    "page_ms": 1700000000000.0,
}
PRESENTATION_ONLY_SCAN: dict[str, Any] = {
    "labels": ["Alex", "Presentation", "Leave call"],
    "page_ms": 1700000000000.0,
}
BANNER_BOX: dict[str, float] = {"x": 636.0, "y": 14.0, "width": 233.0, "height": 36.0}


class BlockingShotPage:
    def __init__(self, gate: asyncio.Event, scan: dict[str, Any]) -> None:
        self._gate = gate
        self._scan = scan
        self.evaluates = 0

    async def evaluate(self, script: str) -> Any:
        self.evaluates += 1
        if script == VISUAL_SCAN_JS:
            return dict(self._scan)
        return None

    async def screenshot(self, **_kwargs: Any) -> bytes:
        await self._gate.wait()
        return shot_bytes()


class _BoxHangingPage:
    def __init__(self) -> None:
        self.cancelled = False

    async def evaluate(self, script: str) -> Any:
        if script == VISUAL_SCAN_JS:
            return dict(PRESENT_SCAN)
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("box evaluate returned")

    async def screenshot(self, **_kwargs: Any) -> bytes:
        return shot_bytes()


class _ShotHangingPage:
    def __init__(self) -> None:
        self.cancelled = False

    async def evaluate(self, script: str) -> Any:
        if script == VISUAL_SCAN_JS:
            return dict(PRESENT_SCAN)
        return None

    async def screenshot(self, **_kwargs: Any) -> bytes:
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("screenshot returned")


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _file_exists(path: str) -> bool:
    return Path(path).exists()


async def test_capture_writes_frame_and_sidecar_row(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage(
        [ABSENT_SCAN, PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png]
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None
    assert await observer.poll() is None
    frame = await observer.poll()
    assert frame is not None
    assert page.shot_kwargs == [{"clip": CLIP_BOX, "type": "jpeg", "quality": 60}]
    raw = await asyncio.to_thread(_read_bytes, frame.path)
    assert raw[:2] == b"\xff\xd8"
    assert await asyncio.to_thread(_file_exists, frame.thumb_path)
    assert frame.sha256 == hashlib.sha256(raw).hexdigest()
    assert frame.dhash == dhash_image(raw)
    assert frame.captured_page_ms == 1700000000000.0
    rows = (tmp_path / "visuals.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    assert row["visual_id"] == frame.frame_id
    assert row["kind"] == "presentation"
    assert row["captured_page_ms"] == 1700000000000.0
    assert row["visible_to"] is None
    assert row["visible_to_page_ms"] is None
    assert row["sha256"] == frame.sha256
    captured = journal.events("visual_captured")
    assert len(captured) == 1
    assert captured[0]["visual_id"] == frame.frame_id
    assert captured[0]["chars"] == len(raw)
    assert captured[0]["duration_ms"] >= 0
    assert captured[0]["capture"] == "clip"


async def test_poll_bounds_a_hung_scan_evaluate(tmp_path: Path) -> None:
    page = HangingPage()
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        schedule=PollSchedule(interval_seconds=0.01),
    )

    assert await observer.poll() is None

    assert page.cancelled is True
    assert observer.presenting is False


async def test_capture_bounds_a_hung_content_box_evaluate(tmp_path: Path) -> None:
    page = _BoxHangingPage()
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        schedule=PollSchedule(interval_seconds=0.01),
    )
    assert await observer.poll() is None

    frame = await observer.poll()

    assert frame is not None
    assert page.cancelled is True
    [captured] = journal.events("visual_captured")
    assert captured["capture"] == "full"


async def test_capture_bounds_a_hung_screenshot(tmp_path: Path) -> None:
    page = _ShotHangingPage()
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        schedule=PollSchedule(interval_seconds=0.01),
    )
    assert await observer.poll() is None

    frame = await asyncio.wait_for(observer.poll(), 1.5)

    assert frame is None
    assert page.cancelled is True


async def test_capture_schedules_eager_caption(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    received: list[tuple[str, str]] = []
    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        captions=CaptionSink(
            captioner,
            lambda frame, caption: received.append((frame.frame_id, caption.text)),
        ),
    )
    assert await observer.poll() is None
    frame = await observer.poll()
    assert frame is not None
    await observer.drain_eager()

    assert len(model.seen) == 1
    assert received == [(frame.frame_id, "A launch timeline.")]
    await observer.close()


async def test_caption_failure_is_journaled_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    observer = VisualObserver(
        page, store, sinks=ConsumerSink(journal=journal), captions=CaptionSink(captioner)
    )

    async def failing(_frame: object) -> object:
        raise RuntimeError("llm down")

    monkeypatch.setattr(captioner, "caption_frame", failing)
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    assert observer.caption_failures == 1
    assert observer.last_caption_error == "RuntimeError"
    [row] = journal.events("visual_caption_failed")
    assert row["stage"] == "caption"
    assert row["error_type"] == "RuntimeError"
    await observer.close()


async def test_caption_emit_failure_is_journaled_and_counted(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))

    def failing_emit(_frame: object, _caption: object) -> None:
        raise RuntimeError("record gone")

    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        captions=CaptionSink(captioner, failing_emit),
    )
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    assert observer.caption_failures == 1
    assert observer.last_caption_error == "RuntimeError"
    [row] = journal.events("visual_caption_failed")
    assert row["stage"] == "emit"
    assert row["error_type"] == "RuntimeError"
    await observer.close()


async def test_duplicate_capture_skips_eager_caption(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png, png]
    )
    store = VisualStore(tmp_path)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    received: list[tuple[str, str]] = []
    observer = VisualObserver(
        page,
        store,
        captions=CaptionSink(
            captioner,
            lambda frame, caption: received.append((frame.frame_id, caption.text)),
        ),
    )
    assert await observer.poll() is None
    first = await observer.poll()
    second = await observer.poll()
    assert first is not None
    assert second is not None
    await observer.drain_eager()

    assert store.duplicates_suppressed == 1
    assert len(model.seen) == 1
    assert received == [(first.frame_id, "A launch timeline.")]
    await observer.close()


async def test_duplicate_poll_extends_visible_range(tmp_path: Path) -> None:
    png = shot_bytes()
    early = dict(PRESENT_SCAN, page_ms=1700000000000.0)
    late = dict(PRESENT_SCAN, page_ms=1700000002000.0)
    page = FakeShotPage(
        [early, early, late], boxes=[dict(CLIP_BOX), dict(CLIP_BOX)], shots=[png, png]
    )
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store)
    assert await observer.poll() is None
    first = await observer.poll()
    assert first is not None
    second = await observer.poll()
    assert second is not None
    assert store.duplicates_suppressed == 1
    [current] = store.frames
    assert current.frame_id == first.frame_id
    assert current.visible_to_page_ms == 1700000002000.0
    assert current.visible_to_at == second.captured_at
    raw = [
        json.loads(line)
        for line in (tmp_path / "visuals.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [row["kind"] for row in raw] == ["presentation", "range"]
    [folded] = read_visual_sidecar(tmp_path / "visuals.jsonl")
    assert folded["visual_id"] == first.frame_id
    assert folded["visible_to"] == second.captured_at
    assert folded["visible_to_page_ms"] == 1700000002000.0
    await observer.close()


async def test_shot_failure_skips_capture(tmp_path: Path) -> None:
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[PlaywrightError("shot gone")],
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    assert await observer.poll() is None
    assert store.frames == ()
    assert journal.events("visual_captured") == []
    await observer.close()


async def test_anchored_clip_used_for_capture(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    frame = await observer.poll()
    assert frame is not None
    assert page.shot_kwargs == [{"clip": CLIP_BOX, "type": "jpeg", "quality": 60}]
    assert journal.events("visual_captured")[0]["capture"] == "clip"
    await observer.close()


async def test_missing_anchor_falls_back_to_full_page(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[None], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    frame = await observer.poll()
    assert frame is not None
    assert page.shot_kwargs == [{"type": "jpeg", "quality": 60}]
    assert journal.events("visual_captured")[0]["capture"] == "full"
    await observer.close()


async def test_tiny_box_falls_back_to_full_page(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(BANNER_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    frame = await observer.poll()
    assert frame is not None
    assert page.shot_kwargs == [{"type": "jpeg", "quality": 60}]
    assert journal.events("visual_captured")[0]["capture"] == "full"
    await observer.close()


async def test_presentation_without_unpin_does_not_capture(tmp_path: Path) -> None:
    page = FakeShotPage(
        [PRESENTATION_ONLY_SCAN, PRESENTATION_ONLY_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[shot_bytes()],
    )
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store)
    assert await observer.poll() is None

    assert await observer.poll() is None
    assert observer.presenting is False
    assert page.shot_kwargs == []
    await observer.close()


def test_content_box_script_never_matches_class_names() -> None:
    assert VISUAL_MIN_CLIP_WIDTH == 400
    assert VISUAL_MIN_CLIP_HEIGHT == 300
    assert "aria-label" in VISUAL_CONTENT_BOX_JS
    assert "unpin" in VISUAL_CONTENT_BOX_JS.lower()
    assert "presentation" in VISUAL_CONTENT_BOX_JS.lower()
    assert "getBoundingClientRect" in VISUAL_CONTENT_BOX_JS
    assert "400" in VISUAL_CONTENT_BOX_JS
    assert "300" in VISUAL_CONTENT_BOX_JS
    assert "class" not in VISUAL_CONTENT_BOX_JS.lower()
    assert "tTdl5d" not in VISUAL_CONTENT_BOX_JS
    assert "H0YpEc" not in VISUAL_CONTENT_BOX_JS


async def test_capture_never_blocks_reply(tmp_path: Path) -> None:
    gate = asyncio.Event()
    page = BlockingShotPage(gate, PRESENT_SCAN)
    store = VisualStore(tmp_path)
    clock = [1000.0]
    observer = VisualObserver(
        page,
        store,
        schedule=PollSchedule(interval_seconds=0.01, monotonic=lambda: clock[0]),
    )
    await observer.start()
    await asyncio.sleep(0.05)

    async def read_latest() -> str:
        frames = store.frames
        return frames[-1].frame_id if frames else "no visual yet"

    started = clock[0]
    reply = await asyncio.wait_for(read_latest(), timeout=2.0)
    elapsed = clock[0] - started
    assert reply == "no visual yet"
    assert elapsed < 1.0
    gate.set()
    await asyncio.wait_for(observer.close(), timeout=1.5)
    calls = page.evaluates
    await asyncio.sleep(0.05)
    assert page.evaluates == calls


def test_prepare_frame_extracts_metadata_off_loop(tmp_path: Path) -> None:
    import hashlib
    import io as io_module

    from PIL import Image

    from fakeyou.visual.visual_observer import prepare_frame
    from fakeyou.visual.visual_store import dhash_image

    raw = io_module.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(raw, format="PNG")
    frame, jpeg_bytes = prepare_frame(raw.getvalue(), 1700000000000.0, tmp_path)

    assert frame.frame_id.startswith("1700000000000-")
    assert frame.captured_page_ms == 1700000000000.0
    assert frame.path.endswith(".jpg")
    assert frame.thumb_path.endswith(".thumb.jpg")
    jpeg = (tmp_path / f"{frame.frame_id}.jpg").read_bytes()
    assert len(jpeg) == jpeg_bytes
    assert frame.sha256 == hashlib.sha256(jpeg).hexdigest()
    assert frame.dhash == dhash_image(jpeg)
    assert (tmp_path / f"{frame.frame_id}.thumb.jpg").is_file()


async def test_observer_capture_leaves_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        calls.append(getattr(func, "__name__", "?"))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)
    png = shot_bytes()
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[png],
    )
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store)

    assert await observer.poll() is None
    assert await observer.poll() is not None
    assert "prepare_frame" in calls
    await observer.close()


def _run_frame_readers(
    writers: list[threading.Thread],
    readers: list[threading.Thread],
    stop: threading.Event,
    collect: Callable[[threading.ExceptHookArgs], None],
) -> None:
    previous_hook = threading.excepthook
    threading.excepthook = collect
    try:
        for worker in [*writers, *readers]:
            worker.start()
        for worker in writers:
            worker.join()
        stop.set()
        for worker in readers:
            worker.join()
    finally:
        threading.excepthook = previous_hook


def test_concurrent_duplicate_prepare_never_exposes_partial_frames(tmp_path: Path) -> None:
    import io as io_module
    import threading

    from PIL import Image

    from fakeyou.visual.visual_observer import prepare_frame

    noise = Image.effect_noise((800, 600), 128).convert("RGB")
    buffer = io_module.BytesIO()
    noise.save(buffer, format="PNG")
    raw = buffer.getvalue()
    page_ms = 1700000000000.0
    frame, _ = prepare_frame(raw, page_ms, tmp_path)
    frame_path = tmp_path / f"{frame.frame_id}.jpg"
    expected = frame_path.read_bytes()
    assert len(expected) > 100_000
    failures: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        for _ in range(60):
            prepare_frame(raw, page_ms, tmp_path)

    def reader() -> None:
        while not stop.is_set():
            if frame_path.read_bytes() != expected:
                raise AssertionError("torn frame read")

    def collect(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            failures.append(args.exc_value)

    writers = [threading.Thread(target=writer) for _ in range(2)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    _run_frame_readers(writers, readers, stop, collect)
    assert failures == []


async def test_scan_bug_propagates_from_poll(tmp_path: Path) -> None:
    page = FakeShotPage([ValueError("bug"), PRESENT_SCAN])
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))

    with pytest.raises(ValueError, match="bug"):
        await observer.poll()
    await observer.close()


async def test_box_bug_propagates_from_poll(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[ValueError("bug")], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    with pytest.raises(ValueError, match="bug"):
        await observer.poll()
    await observer.close()


async def test_shot_bug_propagates_from_poll(tmp_path: Path) -> None:
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[ValueError("bug")],
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    with pytest.raises(ValueError, match="bug"):
        await observer.poll()
    await observer.close()


async def test_emit_bug_propagates_to_the_eager_boundary(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))

    def buggy_emit(_frame: object, _caption: object) -> None:
        raise ValueError("bug")

    observer = VisualObserver(
        page,
        store,
        sinks=ConsumerSink(journal=journal),
        captions=CaptionSink(captioner, buggy_emit),
    )
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    rows = journal.events("visual_caption_failed")
    assert [row["stage"] for row in rows] == ["eager"]
    assert rows[0]["error_type"] == "ValueError"
    await observer.close()


async def test_unexpected_caption_failure_is_retrieved_at_eager_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    observer = VisualObserver(
        page, store, sinks=ConsumerSink(journal=journal), captions=CaptionSink(captioner)
    )

    async def buggy(_frame: object) -> object:
        raise ValueError("bug")

    monkeypatch.setattr(captioner, "caption_frame", buggy)
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    rows = journal.events("visual_caption_failed")
    assert [row["stage"] for row in rows] == ["eager"]
    assert rows[0]["error_type"] == "ValueError"
    await observer.close()


async def test_provider_fault_caption_is_counted_not_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.observability.provider_faults import ProviderFault

    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    observer = VisualObserver(
        page, store, sinks=ConsumerSink(journal=journal), captions=CaptionSink(captioner)
    )

    async def faulting(_frame: object) -> object:
        raise ProviderFault("llm", "unavailable")

    monkeypatch.setattr(captioner, "caption_frame", faulting)
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    assert observer.caption_failures == 1
    assert observer.last_caption_error == "ProviderFault"
    [row] = journal.events("visual_caption_failed")
    assert row["stage"] == "caption"
    await observer.close()


async def test_fakeyou_caption_error_is_counted_not_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.catalog.errors import ErrorCode, FakeYouError

    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], boxes=[dict(CLIP_BOX)], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    model = caption_model("A launch timeline.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    observer = VisualObserver(
        page, store, sinks=ConsumerSink(journal=journal), captions=CaptionSink(captioner)
    )

    async def faulting(_frame: object) -> object:
        raise FakeYouError(ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED)

    monkeypatch.setattr(captioner, "caption_frame", faulting)
    assert await observer.poll() is None
    assert await observer.poll() is not None
    await observer.drain_eager()

    assert observer.caption_failures == 1
    assert observer.last_caption_error == "FakeYouError"
    [row] = journal.events("visual_caption_failed")
    assert row["stage"] == "caption"
    await observer.close()


async def test_scan_failure_is_journaled_and_counted(tmp_path: Path) -> None:
    page = FakeShotPage([PlaywrightError("scan gone")])
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))

    assert await observer.poll() is None

    assert observer.capture_failures == 1
    assert observer.last_capture_error == "Error"
    [row] = journal.events("visual_capture_failed")
    assert row["stage"] == "scan"
    assert row["error_type"] == "Error"
    await observer.close()


async def test_shot_failure_is_journaled_and_counted(tmp_path: Path) -> None:
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[PlaywrightError("shot gone")],
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None

    assert await observer.poll() is None

    assert observer.capture_failures == 1
    assert observer.last_capture_error == "Error"
    [row] = journal.events("visual_capture_failed")
    assert row["stage"] == "screenshot"
    assert row["error_type"] == "Error"
    await observer.close()


def _seeded_png(seed: int) -> bytes:
    image = Image.frombytes("L", (64, 48), random.Random(seed).randbytes(64 * 48)).convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def test_presenting_scan_keeps_observing_across_slide_changes(tmp_path: Path) -> None:
    first = _seeded_png(7)
    second = _seeded_png(8)
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN, PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX), dict(CLIP_BOX), dict(CLIP_BOX)],
        shots=[first, second, second],
    )
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store)

    assert await observer.poll() is None
    assert await observer.poll() is not None
    assert await observer.poll() is not None
    assert await observer.poll() is not None

    assert len(store.frames) == 2
    assert store.duplicates_suppressed == 1
    await observer.close()
