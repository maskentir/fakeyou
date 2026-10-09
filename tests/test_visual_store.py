from __future__ import annotations

import asyncio
import io
import json
import random
import re
from pathlib import Path
from typing import Any

from PIL import Image

from fakeyou.visual_observer import VisualObserver
from fakeyou.visual_store import (
    VISUAL_JOIN_TOLERANCE_MS,
    VisualFrame,
    VisualStore,
    append_visual,
    frames_in_window,
    hamming_distance,
    visual_refs_for_utterance,
)
from tests.fakes import RecordingJournal

PRESENT_SCAN: dict[str, Any] = {
    "labels": [
        "Alex is presenting",
        "Presentation",
        "Unpin Alex's presentation from your main screen",
    ],
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


def _file_exists(path: str) -> bool:
    return Path(path).exists()


def timed_frame(
    frame_id: str, directory: Path, dhash: int, page_ms: float, captured_at: str
) -> VisualFrame:
    path = directory / f"{frame_id}.jpg"
    thumb_path = directory / f"{frame_id}.thumb.jpg"
    path.write_bytes(b"frame-bytes-" + frame_id.encode())
    thumb_path.write_bytes(b"thumb-bytes-" + frame_id.encode())
    return VisualFrame(
        frame_id=frame_id,
        captured_page_ms=page_ms,
        captured_at=captured_at,
        sha256="ab" * 32,
        path=str(path),
        thumb_path=str(thumb_path),
        dhash=dhash,
    )


def window_frames(directory: Path) -> list[VisualFrame]:
    return [
        timed_frame(
            "frame-launch", directory, 0x0000000000000000, 100000.0, "2026-10-09T03:10:00+00:00"
        ),
        timed_frame(
            "frame-pricing", directory, 0xFFFFFFFFFFFFFFFF, 110000.0, "2026-10-09T03:10:10+00:00"
        ),
        timed_frame(
            "frame-roadmap", directory, 0xAAAAAAAAAAAAAAAA, 130000.0, "2026-10-09T03:10:30+00:00"
        ),
    ]


def stored_frame(
    frame_id: str, directory: Path, dhash: int, page_ms: float = 1700000000000.0
) -> VisualFrame:
    path = directory / f"{frame_id}.jpg"
    thumb_path = directory / f"{frame_id}.thumb.jpg"
    path.write_bytes(b"frame-bytes-" + frame_id.encode())
    thumb_path.write_bytes(b"thumb-bytes-" + frame_id.encode())
    return VisualFrame(
        frame_id=frame_id,
        captured_page_ms=page_ms,
        captured_at="2026-10-09T00:00:00+00:00",
        sha256="ab" * 32,
        path=str(path),
        thumb_path=str(thumb_path),
        dhash=dhash,
    )


async def test_dedup_drops_near_duplicate(tmp_path: Path) -> None:
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    first = stored_frame("0000000000001-aaaabbbb", tmp_path, 0)
    append_visual(store, first)
    assert hamming_distance(0, 0xFF) == 8
    second = stored_frame("0000000000002-ccccdddd", tmp_path, 0xFF)
    append_visual(store, second)
    assert [frame.frame_id for frame in store.frames] == [first.frame_id]
    assert store.duplicates_suppressed == 1
    assert not await asyncio.to_thread(_file_exists, second.path)
    assert hamming_distance(0, 0x1FF) == 9
    third = stored_frame("0000000000003-eeeeffff", tmp_path, 0x1FF)
    append_visual(store, third)
    assert [frame.frame_id for frame in store.frames] == [first.frame_id, third.frame_id]
    assert store.duplicates_suppressed == 1
    rows = (tmp_path / "visuals.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    suppressed = journal.events("duplicate_suppressed")
    assert len(suppressed) == 1
    assert suppressed[0]["visual_id"] == second.frame_id
    assert suppressed[0]["matched_id"] == first.frame_id
    assert suppressed[0]["hamming"] == 8


async def test_ring_spills_to_disk(tmp_path: Path) -> None:
    store = VisualStore(tmp_path)
    rng = random.Random(11)
    hashes = [rng.getrandbits(64) for _ in range(91)]
    for left in range(91):
        for right in range(left + 1, 91):
            assert hamming_distance(hashes[left], hashes[right]) > 8
    frames = [stored_frame(f"ring-{index:03d}", tmp_path, hashes[index]) for index in range(91)]
    for frame in frames:
        append_visual(store, frame)
    assert len(store.frames) == 90
    assert [frame.frame_id for frame in store.frames] == [
        f"ring-{index:03d}" for index in range(1, 91)
    ]
    assert store.duplicates_suppressed == 0
    rows = (tmp_path / "visuals.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 91
    assert json.loads(rows[0])["visual_id"] == "ring-000"
    assert await asyncio.to_thread(_file_exists, frames[0].path)


def test_join_tolerance_matches_spec_window() -> None:
    assert VISUAL_JOIN_TOLERANCE_MS == 2000


def test_frames_in_window_selects_spec_range(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert [frame.frame_id for frame in frames_in_window(frames, 99500.0, 100500.0)] == [
        "frame-launch"
    ]
    assert [frame.frame_id for frame in frames_in_window(frames, 99000.0, 111000.0)] == [
        "frame-launch",
        "frame-pricing",
    ]
    assert frames_in_window(frames, 150000.0, 151000.0) == []


def test_frames_in_window_edges_are_inclusive(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert [frame.frame_id for frame in frames_in_window(frames, 102000.0, 108000.0)] == [
        "frame-launch",
        "frame-pricing",
    ]


def test_refs_for_utterance_uses_capture_join(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert visual_refs_for_utterance(frames, capture_ms=100000.0) == ("frame-launch",)
    assert visual_refs_for_utterance(frames, capture_ms=109000.0) == ("frame-pricing",)
    assert visual_refs_for_utterance(frames, capture_ms=115000.0) == ()


def test_refs_for_utterance_falls_back_to_wall_clock(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert visual_refs_for_utterance(frames, spoken_at="2026-10-09T03:10:11+00:00") == (
        "frame-pricing",
    )
    assert visual_refs_for_utterance(frames, spoken_at="2026-10-09T03:14:00+00:00") == ()


def test_refs_for_utterance_rejects_unparseable_stamp(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert visual_refs_for_utterance(frames, spoken_at="soon") == ()
    assert visual_refs_for_utterance(frames) == ()


async def test_journal_has_no_bytes(tmp_path: Path) -> None:
    tile = shot_bytes((200, 40, 20))
    first_scan = dict(PRESENT_SCAN, page_ms=1700000001000.0)
    second_scan = dict(PRESENT_SCAN, page_ms=1700000003000.0)
    page = FakeShotPage(
        [first_scan, first_scan, second_scan],
        shots=[tile, tile],
        box={"x": 8.0, "y": 8.0, "width": 24.0, "height": 24.0},
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, journal=journal)
    assert await observer.poll() is None
    first = await observer.poll()
    assert first is not None
    assert page.full_calls == 0
    second = await observer.poll()
    assert second is not None
    assert page.full_calls == 0
    assert len(store.frames) == 1
    assert store.duplicates_suppressed == 1
    assert journal.events("visual_captured")
    assert journal.events("duplicate_suppressed")
    for line in journal.lines:
        assert "data:image" not in line
        assert "base64" not in line.lower()
        assert len(line.encode()) < 4096
        assert re.search(r"[A-Za-z0-9+/=]{100,}", line) is None
