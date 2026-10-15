from __future__ import annotations

import asyncio
import io
import random
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from fakeyou.observability.faults import ConsumerSink
from fakeyou.visual.visual_observer import VISUAL_SCAN_JS, VisualObserver
from fakeyou.visual.visual_store import (
    VISUAL_JOIN_TOLERANCE_MS,
    VisualFrame,
    VisualStore,
    append_visual,
    frames_in_window,
    hamming_distance,
    read_visual_sidecar,
    visual_refs_for_utterance,
)
from tests._persistence_fakes import RecordingJournal

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

    async def evaluate(self, script: str) -> Any:
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
    store = VisualStore(directory)
    rows = [
        ("frame-launch", 0x0000000000000000, 100000.0, "2026-10-09T03:10:00+00:00"),
        ("frame-pricing", 0xFFFFFFFFFFFFFFFF, 110000.0, "2026-10-09T03:10:10+00:00"),
        ("frame-roadmap", 0xAAAAAAAAAAAAAAAA, 130000.0, "2026-10-09T03:10:30+00:00"),
    ]
    for frame_id, dhash, page_ms, stamp in rows:
        store.append(timed_frame(frame_id, directory, dhash, page_ms, stamp))
    assert len(store.frames) == 3
    return list(store.frames)


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
    assert len(read_visual_sidecar(tmp_path / "visuals.jsonl")) == 2
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
    frames_on_disk = read_visual_sidecar(tmp_path / "visuals.jsonl")
    assert len(frames_on_disk) == 91
    assert frames_on_disk[0]["visual_id"] == "ring-000"
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
    assert [frame.frame_id for frame in frames_in_window(frames, 150000.0, 151000.0)] == [
        "frame-roadmap"
    ]


def test_frames_in_window_edges_are_inclusive(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert [frame.frame_id for frame in frames_in_window(frames, 102000.0, 108000.0)] == [
        "frame-launch",
        "frame-pricing",
    ]


def test_refs_for_utterance_uses_capture_join(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert visual_refs_for_utterance(frames, capture_ms=100000.0) == ("frame-launch",)
    assert visual_refs_for_utterance(frames, capture_ms=109000.0) == (
        "frame-launch",
        "frame-pricing",
    )
    assert visual_refs_for_utterance(frames, capture_ms=115000.0) == ("frame-pricing",)


def test_refs_for_utterance_falls_back_to_wall_clock(tmp_path: Path) -> None:
    frames = window_frames(tmp_path)

    assert visual_refs_for_utterance(frames, spoken_at="2026-10-09T03:10:11+00:00") == (
        "frame-launch",
        "frame-pricing",
    )
    assert visual_refs_for_utterance(frames, spoken_at="2026-10-09T03:14:00+00:00") == (
        "frame-roadmap",
    )


def test_static_slide_asked_late_still_cites(tmp_path: Path) -> None:
    store = VisualStore(tmp_path)
    slide = timed_frame(
        "static-slide", tmp_path, 0x0000000000000000, 1000000.0, "2026-10-09T23:13:43+00:00"
    )
    store.append(slide)
    polls = [
        ("static-dup-1", 0xFF, 1002000.0, "2026-10-09T23:13:45+00:00"),
        ("static-dup-2", 0x03, 1004000.0, "2026-10-09T23:13:47+00:00"),
        ("static-dup-3", 0x0F, 1006000.0, "2026-10-09T23:13:49+00:00"),
    ]
    for frame_id, dhash, page_ms, stamp in polls:
        store.append(timed_frame(frame_id, tmp_path, dhash, page_ms, stamp))
    assert store.duplicates_suppressed == 3
    assert visual_refs_for_utterance(store.frames, capture_ms=1008000.0) == ("static-slide",)
    assert visual_refs_for_utterance(store.frames, spoken_at="2026-10-09T23:13:51+00:00") == (
        "static-slide",
    )
    [current] = store.frames
    assert current.visible_to_page_ms == 1006000.0
    assert current.visible_to_at == "2026-10-09T23:13:49+00:00"
    [folded] = read_visual_sidecar(tmp_path / "visuals.jsonl")
    assert folded["visual_id"] == "static-slide"
    assert folded["visible_to"] == "2026-10-09T23:13:49+00:00"
    assert folded["visible_to_page_ms"] == 1006000.0


def test_replaced_frame_outside_range_yields_nothing(tmp_path: Path) -> None:
    store = VisualStore(tmp_path)
    store.append(
        timed_frame(
            "range-first", tmp_path, 0x0000000000000000, 200000.0, "2026-10-09T03:10:00+00:00"
        )
    )
    store.append(
        timed_frame(
            "range-second", tmp_path, 0xFFFFFFFFFFFFFFFF, 210000.0, "2026-10-09T03:10:10+00:00"
        )
    )
    assert visual_refs_for_utterance(store.frames, capture_ms=230000.0) == ("range-second",)
    assert visual_refs_for_utterance(store.frames, spoken_at="2026-10-09T03:10:30+00:00") == (
        "range-second",
    )
    assert visual_refs_for_utterance(store.frames, capture_ms=150000.0) == ()
    assert visual_refs_for_utterance(store.frames, spoken_at="2026-10-09T03:09:30+00:00") == ()
    first, second = store.frames
    assert first.visible_to_page_ms == 210000.0
    assert first.visible_to_at == "2026-10-09T03:10:10+00:00"
    assert second.visible_to_page_ms is None
    assert second.visible_to_at is None
    rows = {row["visual_id"]: row for row in read_visual_sidecar(tmp_path / "visuals.jsonl")}
    assert rows["range-first"]["visible_to"] == "2026-10-09T03:10:10+00:00"
    assert rows["range-first"]["visible_to_page_ms"] == 210000.0
    assert rows["range-second"]["visible_to"] is None
    assert rows["range-second"]["visible_to_page_ms"] is None


def test_transition_utterance_yields_both_frames(tmp_path: Path) -> None:
    store = VisualStore(tmp_path)
    store.append(
        timed_frame(
            "swap-before", tmp_path, 0x0000000000000000, 300000.0, "2026-10-09T03:10:00+00:00"
        )
    )
    store.append(
        timed_frame(
            "swap-after", tmp_path, 0xFFFFFFFFFFFFFFFF, 310000.0, "2026-10-09T03:10:10+00:00"
        )
    )
    assert visual_refs_for_utterance(store.frames, capture_ms=310000.0) == (
        "swap-before",
        "swap-after",
    )
    assert visual_refs_for_utterance(store.frames, spoken_at="2026-10-09T03:10:10+00:00") == (
        "swap-before",
        "swap-after",
    )


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
        boxes=[{"x": 173.0, "y": 65.0, "width": 592.0, "height": 385.0}],
        shots=[tile, tile],
    )
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    observer = VisualObserver(page, store, sinks=ConsumerSink(journal=journal))
    assert await observer.poll() is None
    first = await observer.poll()
    assert first is not None
    assert "clip" in page.shot_kwargs[0]
    second = await observer.poll()
    assert second is not None
    assert "clip" in page.shot_kwargs[1]
    assert len(store.frames) == 1
    assert store.duplicates_suppressed == 1
    assert journal.events("visual_captured")
    assert journal.events("duplicate_suppressed")
    for line in journal.lines:
        assert "data:image" not in line
        assert "base64" not in line.lower()
        assert len(line.encode()) < 4096
        assert re.search(r"[A-Za-z0-9+/=]{100,}", line) is None


def test_persist_range_read_failure_is_journaled(tmp_path: Path) -> None:
    journal = RecordingJournal()
    store = VisualStore(tmp_path, journal=journal)
    first = timed_frame(
        "frame-a", tmp_path, 0x0000000000000000, 100000.0, "2026-10-09T03:10:00+00:00"
    )
    store.append(first)
    store.sidecar.unlink()
    store.sidecar.mkdir()
    second = timed_frame(
        "frame-b", tmp_path, 0xFFFFFFFFFFFFFFFF, 110000.0, "2026-10-09T03:10:10+00:00"
    )

    with pytest.raises(OSError, match="Is a directory"):
        store.append(second)

    [row] = journal.events("visual_range_persist_failed")
    assert row["frame_id"] == "frame-a"
    assert row["error_type"] == "IsADirectoryError"
