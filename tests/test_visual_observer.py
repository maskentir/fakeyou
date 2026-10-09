from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
from typing import Any

from PIL import Image

from fakeyou.settings import VisualSettings
from fakeyou.visual_caption import VisualCaptioner
from fakeyou.visual_observer import (
    VISUAL_CONTENT_BOX_JS,
    VISUAL_MIN_CLIP_HEIGHT,
    VISUAL_MIN_CLIP_WIDTH,
    VISUAL_SCAN_JS,
    VisualObserver,
)
from fakeyou.visual_store import VisualStore, dhash_image
from tests.fakes import RecordingJournal
from tests.test_visual_caption import caption_model

PRESENT_SCAN: dict[str, Any] = {
    "labels": [
        "Alex is presenting",
        "Presentation",
        "Unpin Alex's presentation from your main screen",
    ],
    "page_ms": 1700000000000.0,
}
ABSENT_SCAN: dict[str, Any] = {
    "labels": ["Alex", "Leave call"],
    "page_ms": 1700000000000.0,
}
PRESENTATION_ONLY_SCAN: dict[str, Any] = {
    "labels": ["Alex", "Presentation", "Leave call"],
    "page_ms": 1700000000000.0,
}
CLIP_BOX: dict[str, float] = {"x": 173.0, "y": 65.0, "width": 592.0, "height": 385.0}
BANNER_BOX: dict[str, float] = {"x": 636.0, "y": 14.0, "width": 233.0, "height": 36.0}


def shot_bytes(color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeShotPage:
    def __init__(
        self,
        scans: list[Any],
        boxes: list[Any] | None = None,
        shots: list[bytes | Exception] | None = None,
    ) -> None:
        self._scans = list(scans)
        self._boxes = list(boxes or [])
        self._shots = list(shots or [])
        self.shot_kwargs: list[dict[str, Any]] = []
        self.evaluate_calls: list[str] = []

    async def evaluate(self, script: str) -> Any:
        self.evaluate_calls.append(script)
        if script == VISUAL_SCAN_JS:
            if not self._scans:
                raise AssertionError("scan evaluate called more times than scripted")
            if len(self._scans) > 1:
                return self._scans.pop(0)
            return self._scans[0]
        if not self._boxes:
            raise AssertionError("box evaluate called more times than scripted")
        if len(self._boxes) > 1:
            return self._boxes.pop(0)
        return self._boxes[0]

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.shot_kwargs.append(kwargs)
        if not self._shots:
            raise AssertionError("page screenshot called more times than scripted")
        result = self._shots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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

    async def screenshot(self, **kwargs: Any) -> bytes:
        await self._gate.wait()
        return shot_bytes()


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
    observer = VisualObserver(page, store, journal=journal)
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
    assert row["sha256"] == frame.sha256
    captured = journal.events("visual_captured")
    assert len(captured) == 1
    assert captured[0]["visual_id"] == frame.frame_id
    assert captured[0]["chars"] == len(raw)
    assert captured[0]["duration_ms"] >= 0
    assert captured[0]["capture"] == "clip"


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
        journal=journal,
        captioner=captioner,
        on_caption=lambda frame, caption: received.append((frame.frame_id, caption.text)),
    )
    assert await observer.poll() is None
    frame = await observer.poll()
    assert frame is not None
    await observer.drain_eager()

    assert len(model.seen) == 1
    assert received == [(frame.frame_id, "A launch timeline.")]
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
        captioner=captioner,
        on_caption=lambda frame, caption: received.append((frame.frame_id, caption.text)),
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


async def test_shot_failure_skips_capture(tmp_path: Path) -> None:
    page = FakeShotPage(
        [PRESENT_SCAN, PRESENT_SCAN],
        boxes=[dict(CLIP_BOX)],
        shots=[RuntimeError("shot gone")],
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store, journal=journal)
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
    observer = VisualObserver(page, store, journal=journal)
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
    observer = VisualObserver(page, store, journal=journal)
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
    observer = VisualObserver(page, store, journal=journal)
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
    observer = VisualObserver(page, store, monotonic=lambda: clock[0], poll_interval_seconds=0.01)
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
