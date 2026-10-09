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
from fakeyou.visual_observer import VisualObserver
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


def shot_bytes(color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    image = Image.new("RGB", (64, 48), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeShotLocator:
    def __init__(
        self,
        page: FakeShotPage,
        selector: str,
        shots: list[bytes | Exception],
        box: dict[str, float] | None,
    ) -> None:
        self._page = page
        self.selector = selector
        self._shots = shots
        self._box = box

    async def screenshot(self, **kwargs: Any) -> bytes:
        self._page.shot_kwargs.append(kwargs)
        if not self._shots:
            raise AssertionError("tile screenshot called more times than scripted")
        result = self._shots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def bounding_box(self) -> dict[str, float] | None:
        return self._box


class FakeShotPage:
    def __init__(
        self,
        scans: list[Any],
        shots: list[bytes | Exception] | None = None,
        full: bytes | Exception | None = None,
        box: dict[str, float] | None = None,
    ) -> None:
        self._scans = list(scans)
        self._shots = list(shots or [])
        self._full = full
        self._box = box
        self.locator_calls: list[str] = []
        self.shot_kwargs: list[dict[str, Any]] = []
        self.full_calls = 0

    async def evaluate(self, _script: str) -> Any:
        if not self._scans:
            raise AssertionError("page evaluate called more times than scripted")
        if len(self._scans) > 1:
            return self._scans.pop(0)
        return self._scans[0]

    def locator(self, selector: str) -> FakeShotLocator:
        self.locator_calls.append(selector)
        return FakeShotLocator(self, selector, self._shots, self._box)

    async def screenshot(self, **_kwargs: Any) -> bytes:
        self.full_calls += 1
        if self._full is None:
            raise AssertionError("full page screenshot was not scripted")
        if isinstance(self._full, Exception):
            raise self._full
        return self._full


class BlockingShotPage:
    def __init__(self, gate: asyncio.Event, scan: dict[str, Any]) -> None:
        self._gate = gate
        self._scan = scan
        self.evaluates = 0
        self.locator_calls = 0

    async def evaluate(self, _script: str) -> Any:
        self.evaluates += 1
        return dict(self._scan)

    def locator(self, _selector: str) -> BlockingShotLocator:
        self.locator_calls += 1
        return BlockingShotLocator(self._gate)

    async def screenshot(self, **_kwargs: Any) -> bytes:
        await self._gate.wait()
        return shot_bytes()


class BlockingShotLocator:
    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def screenshot(self, **kwargs: Any) -> bytes:
        await self._gate.wait()
        return shot_bytes()

    async def bounding_box(self) -> None:
        return None


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _file_exists(path: str) -> bool:
    return Path(path).exists()


async def test_capture_writes_frame_and_sidecar_row(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([ABSENT_SCAN, PRESENT_SCAN, PRESENT_SCAN], shots=[png])
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, journal=journal)
    assert await observer.poll() is None
    assert await observer.poll() is None
    frame = await observer.poll()
    assert frame is not None
    assert page.locator_calls == ["xpath=//*[@aria-label='Presentation']"]
    assert page.shot_kwargs == [{"type": "jpeg", "quality": 60}]
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


async def test_capture_schedules_eager_caption(tmp_path: Path) -> None:
    png = shot_bytes()
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], shots=[png])
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
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN, PRESENT_SCAN], shots=[png, png])
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


async def test_tile_failure_skips_capture_without_full_page(tmp_path: Path) -> None:
    page = FakeShotPage([PRESENT_SCAN, PRESENT_SCAN], shots=[RuntimeError("tile gone")], full=None)
    store = VisualStore(tmp_path)
    observer = VisualObserver(page, store)
    assert await observer.poll() is None

    assert await observer.poll() is None
    assert page.full_calls == 0
    assert store.frames == ()
    await observer.close()


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
