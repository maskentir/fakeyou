import io
import json
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from fakeyou.journal import Journal

VISUAL_RING_SIZE = 90
VISUAL_DEDUP_HAMMING_MAX = 8
VISUAL_SIDECAR_NAME = "visuals.jsonl"
VISUAL_KIND = "presentation"
VISUAL_FRAME_SUFFIX = ".jpg"
VISUAL_THUMB_SUFFIX = ".thumb.jpg"
VISUAL_JOIN_TOLERANCE_MS = 2000.0


@dataclass(frozen=True, slots=True)
class VisualFrame:
    frame_id: str
    captured_page_ms: float
    captured_at: str
    sha256: str
    path: str
    thumb_path: str
    dhash: int


def dhash_image(data: bytes) -> int:
    with Image.open(io.BytesIO(data)) as handle:
        small = handle.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
        pixels = small.tobytes()
    digest = 0
    for row in range(8):
        for column in range(8):
            left = pixels[row * 9 + column]
            right = pixels[row * 9 + column + 1]
            digest = (digest << 1) | (1 if left > right else 0)
    return digest


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


class VisualStore:
    def __init__(self, directory: Path | str, *, journal: Journal | None = None) -> None:
        self._directory = Path(directory)
        self._journal = journal
        self._frames: deque[VisualFrame] = deque(maxlen=VISUAL_RING_SIZE)
        self._duplicates = 0
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def sidecar(self) -> Path:
        return self._directory / VISUAL_SIDECAR_NAME

    @property
    def frames(self) -> tuple[VisualFrame, ...]:
        return tuple(self._frames)

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates

    def append(self, frame: VisualFrame) -> None:
        for existing in self._frames:
            if hamming_distance(frame.dhash, existing.dhash) <= VISUAL_DEDUP_HAMMING_MAX:
                self._duplicates += 1
                if self._journal is not None:
                    self._journal.record(
                        "duplicate_suppressed",
                        source="browser",
                        visual_id=frame.frame_id,
                        matched_id=existing.frame_id,
                        hamming=hamming_distance(frame.dhash, existing.dhash),
                    )
                self._drop_files(frame)
                return
        self.sidecar.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "visual_id": frame.frame_id,
            "kind": VISUAL_KIND,
            "captured_page_ms": frame.captured_page_ms,
            "captured_at": frame.captured_at,
            "visible_from": frame.captured_at,
            "visible_to": None,
            "sha256": frame.sha256,
            "thumb": Path(frame.thumb_path).name,
        }
        with self.sidecar.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._frames.append(frame)

    def _drop_files(self, frame: VisualFrame) -> None:
        for candidate in (frame.path, frame.thumb_path):
            try:
                shared = any(
                    other.path == candidate or other.thumb_path == candidate
                    for other in self._frames
                )
                if not shared:
                    Path(candidate).unlink(missing_ok=True)
            except OSError:
                pass


def append_visual(store: VisualStore, frame: VisualFrame) -> None:
    store.append(frame)


def frames_in_window(
    frames: Sequence[VisualFrame],
    from_ms: float,
    to_ms: float,
    *,
    tolerance_ms: float = VISUAL_JOIN_TOLERANCE_MS,
) -> list[VisualFrame]:
    low = from_ms - tolerance_ms
    high = to_ms + tolerance_ms
    return [frame for frame in frames if low <= frame.captured_page_ms <= high]


def _wall_ms(stamp: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp() * 1000.0


def visual_refs_for_utterance(
    frames: Sequence[VisualFrame],
    *,
    capture_ms: float = 0.0,
    spoken_at: str = "",
    tolerance_ms: float = VISUAL_JOIN_TOLERANCE_MS,
) -> tuple[str, ...]:
    if isinstance(capture_ms, bool) or capture_ms <= 0.0:
        spoken_ms = _wall_ms(spoken_at)
        if spoken_ms is None:
            return ()
        refs: list[str] = []
        for frame in frames:
            captured_ms = _wall_ms(frame.captured_at)
            if captured_ms is not None and abs(captured_ms - spoken_ms) <= tolerance_ms:
                refs.append(frame.frame_id)
        return tuple(refs)
    return tuple(
        frame.frame_id
        for frame in frames_in_window(frames, capture_ms, capture_ms, tolerance_ms=tolerance_ms)
    )
