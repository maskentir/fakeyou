import asyncio
import hashlib
import io
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from fakeyou.journal import Journal
from fakeyou.visual_caption import VisualCaption, VisualCaptioner
from fakeyou.visual_store import (
    VISUAL_FRAME_SUFFIX,
    VISUAL_KIND,
    VISUAL_THUMB_SUFFIX,
    VisualFrame,
    VisualStore,
    append_visual,
    dhash_image,
)

VISUAL_MIN_CLIP_WIDTH = 400
VISUAL_MIN_CLIP_HEIGHT = 300
VISUAL_CONTENT_BOX_JS = """() => {
  const anchor = () => {
    const nodes = document.querySelectorAll('button[aria-label]');
    for (const node of nodes) {
      const label = (node.getAttribute('aria-label') || '').toLowerCase();
      if (label.startsWith('unpin') && label.includes('presentation')) {
        return node;
      }
    }
    return null;
  };
  let found = null;
  try {
    found = anchor();
  } catch (e) {
    return null;
  }
  if (found === null) {
    return null;
  }
  let node = found.parentElement;
  while (node !== null) {
    let rect = null;
    try {
      rect = node.getBoundingClientRect();
    } catch (e) {
      return null;
    }
    if (rect.width >= 400 && rect.height >= 300) {
      return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
    }
    node = node.parentElement;
  }
  return null;
}"""
VISUAL_POLL_INTERVAL_SECONDS = 2.0
VISUAL_JPEG_QUALITY = 60
VISUAL_THUMB_WIDTH = 320
VISUAL_DEBOUNCE_POLLS = 2
VISUAL_SCAN_JS = """() => {
  const labels = [];
  try {
    const nodes = document.querySelectorAll('[aria-label]');
    for (const node of nodes) {
      labels.push((node.getAttribute('aria-label') || '').slice(0, 160));
      if (labels.length >= 60) break;
    }
  } catch (e) {}
  let page_ms = 0;
  try {
    page_ms = window.performance.timeOrigin + window.performance.now();
  } catch (e) {
    page_ms = Date.now();
  }
  return {labels, page_ms};
}"""


def _scan_labels(scan: Any) -> list[str]:
    if not isinstance(scan, dict):
        return []
    raw = scan.get("labels")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str)]


def _scan_shows_presenting(scan: Any) -> bool:
    labels = _scan_labels(scan)
    if any(item.lower().endswith("is presenting") for item in labels):
        return True
    if not any(item == "Presentation" for item in labels):
        return False
    return any(
        item.lower().startswith("unpin") and "presentation" in item.lower() for item in labels
    )


def _content_box(found: Any) -> dict[str, float] | None:
    if not isinstance(found, dict):
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        value = found.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values[key] = number
    if values["x"] < 0 or values["y"] < 0:
        return None
    if values["width"] < VISUAL_MIN_CLIP_WIDTH or values["height"] < VISUAL_MIN_CLIP_HEIGHT:
        return None
    return values


def _scan_page_ms(scan: Any) -> float:
    if isinstance(scan, dict):
        value = scan.get("page_ms")
        if isinstance(value, bool):
            pass
        elif isinstance(value, (int, float)) and value > 0:
            return float(value)
    return datetime.now(UTC).timestamp() * 1000.0


def _encode_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=VISUAL_JPEG_QUALITY)
    return buffer.getvalue()


def _encode_thumb(image: Image.Image) -> bytes:
    thumb = image
    if image.width > VISUAL_THUMB_WIDTH:
        height = max(1, round(image.height * VISUAL_THUMB_WIDTH / image.width))
        thumb = image.resize((VISUAL_THUMB_WIDTH, height), Image.Resampling.LANCZOS)
    return _encode_jpeg(thumb)


class VisualObserver:
    def __init__(
        self,
        page: Any,
        store: VisualStore,
        *,
        journal: Journal | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        poll_interval_seconds: float = VISUAL_POLL_INTERVAL_SECONDS,
        enabled: bool = True,
        captioner: VisualCaptioner | None = None,
        on_caption: Callable[[VisualFrame, VisualCaption], None] | None = None,
    ) -> None:
        self._page = page
        self._store = store
        self._journal = journal
        self._monotonic = monotonic
        self._poll_interval = poll_interval_seconds
        self._enabled = enabled
        self._captioner = captioner
        self._on_caption = on_caption
        self._hits = 0
        self._presenting = False
        self._task: asyncio.Task[None] | None = None
        self._eager: set[asyncio.Task[None]] = set()
        self._closed = False

    @property
    def presenting(self) -> bool:
        return self._presenting

    async def start(self) -> None:
        if self._task is not None or self._closed:
            return
        self._task = asyncio.create_task(self._poll_loop())

    async def close(self) -> None:
        task, self._task = self._task, None
        self._closed = True
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        eager, self._eager = set(self._eager), set()
        for pending in eager:
            pending.cancel()
        if eager:
            await asyncio.gather(*eager, return_exceptions=True)

    async def drain_eager(self) -> None:
        pending = [task for task in self._eager if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def poll(self) -> VisualFrame | None:
        if not self._enabled:
            return None
        try:
            scan = await self._page.evaluate(VISUAL_SCAN_JS)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._hits = 0
            self._presenting = False
            return None
        presenting = _scan_shows_presenting(scan)
        self._presenting = presenting
        if not presenting:
            self._hits = 0
            return None
        self._hits += 1
        if self._hits < VISUAL_DEBOUNCE_POLLS:
            return None
        return await self._capture(scan)

    async def _poll_loop(self) -> None:
        while True:
            started = self._monotonic()
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            elapsed = self._monotonic() - started
            await asyncio.sleep(max(0.0, self._poll_interval - elapsed))

    async def _caption_and_emit(self, frame: VisualFrame) -> None:
        captioner = self._captioner
        if captioner is None:
            return
        try:
            caption = await captioner.caption_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if caption is None:
            return
        callback = self._on_caption
        if callback is None:
            return
        try:
            callback(frame, caption)
        except Exception:
            pass

    def _schedule_eager_caption(self, frame: VisualFrame) -> None:
        if self._captioner is None:
            return
        task = asyncio.create_task(self._caption_and_emit(frame))
        self._eager.add(task)
        task.add_done_callback(self._eager.discard)

    async def _capture(self, scan: Any) -> VisualFrame | None:
        started = self._monotonic()
        try:
            found = await self._page.evaluate(VISUAL_CONTENT_BOX_JS)
        except asyncio.CancelledError:
            raise
        except Exception:
            found = None
        box = _content_box(found)
        try:
            if box is not None:
                raw = await self._page.screenshot(
                    clip=box, type="jpeg", quality=VISUAL_JPEG_QUALITY
                )
                capture = "clip"
            else:
                raw = await self._page.screenshot(type="jpeg", quality=VISUAL_JPEG_QUALITY)
                capture = "full"
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        image = Image.open(io.BytesIO(bytes(raw))).convert("RGB")
        jpeg = _encode_jpeg(image)
        thumb = _encode_thumb(image)
        digest = hashlib.sha256(jpeg).hexdigest()
        page_ms = _scan_page_ms(scan)
        frame_id = f"{int(page_ms):013d}-{digest[:8]}"
        captured_at = datetime.now(UTC).isoformat()
        frame_path = self._store.directory / f"{frame_id}{VISUAL_FRAME_SUFFIX}"
        thumb_path = self._store.directory / f"{frame_id}{VISUAL_THUMB_SUFFIX}"
        frame_path.write_bytes(jpeg)
        thumb_path.write_bytes(thumb)
        frame = VisualFrame(
            frame_id=frame_id,
            captured_page_ms=page_ms,
            captured_at=captured_at,
            sha256=digest,
            path=str(frame_path),
            thumb_path=str(thumb_path),
            dhash=dhash_image(jpeg),
        )
        duplicates = self._store.duplicates_suppressed
        append_visual(self._store, frame)
        stored = self._store.duplicates_suppressed == duplicates
        if stored:
            self._schedule_eager_caption(frame)
        if self._journal is not None and stored:
            self._journal.record(
                "visual_captured",
                source="browser",
                visual_id=frame_id,
                kind=VISUAL_KIND,
                chars=len(jpeg),
                duration_ms=(self._monotonic() - started) * 1000,
                capture=capture,
            )
        return frame
