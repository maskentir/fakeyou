import asyncio
import hashlib
import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, override

from PIL import Image
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import FloatRect

from fakeyou.catalog.errors import FakeYouError
from fakeyou.foundation.invariants import POLL_TIMEOUT_MULTIPLIER
from fakeyou.foundation.numbers import finite_float, real_number
from fakeyou.foundation.polling import PageEvaluator, PollingLoop, PollSchedule
from fakeyou.observability.faults import ConsumerSink, create_poll_guard
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.persistence.files import write_atomically
from fakeyou.visual.visual_caption import VisualCaption, VisualCaptioner
from fakeyou.visual.visual_store import (
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


class VisualPage(PageEvaluator, Protocol):
    async def screenshot(
        self,
        *,
        type: Literal["jpeg", "png", "webp"] | None = None,
        quality: int | None = None,
        clip: FloatRect | None = None,
    ) -> bytes: ...


def _content_box(found: Any) -> FloatRect | None:
    if not isinstance(found, dict):
        return None
    values: dict[str, float] = {}
    for key in ("x", "y", "width", "height"):
        number = finite_float(found.get(key))
        if number is None:
            return None
        values[key] = number
    if values["x"] < 0 or values["y"] < 0:
        return None
    if values["width"] < VISUAL_MIN_CLIP_WIDTH or values["height"] < VISUAL_MIN_CLIP_HEIGHT:
        return None
    return FloatRect(x=values["x"], y=values["y"], width=values["width"], height=values["height"])


def _scan_page_ms(scan: Any) -> float:
    if isinstance(scan, dict):
        value = real_number(scan.get("page_ms"))
        if value is not None and value > 0:
            return value
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


def prepare_frame(raw: bytes, page_ms: float, directory: Path) -> tuple[VisualFrame, int]:
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    jpeg = _encode_jpeg(image)
    thumb = _encode_thumb(image)
    digest = hashlib.sha256(jpeg).hexdigest()
    frame_id = f"{int(page_ms):013d}-{digest[:8]}"
    captured_at = datetime.now(UTC).isoformat()
    frame_path = directory / f"{frame_id}{VISUAL_FRAME_SUFFIX}"
    thumb_path = directory / f"{frame_id}{VISUAL_THUMB_SUFFIX}"
    write_atomically(frame_path, jpeg)
    write_atomically(thumb_path, thumb)
    frame = VisualFrame(
        frame_id=frame_id,
        captured_page_ms=page_ms,
        captured_at=captured_at,
        sha256=digest,
        path=str(frame_path),
        thumb_path=str(thumb_path),
        dhash=dhash_image(jpeg),
    )
    return frame, len(jpeg)


DEFAULT_POLL_SCHEDULE = PollSchedule(interval_seconds=VISUAL_POLL_INTERVAL_SECONDS)


@dataclass(frozen=True, slots=True)
class CaptionSink:
    captioner: VisualCaptioner | None = None
    on_caption: Callable[[VisualFrame, VisualCaption], None] | None = None


DEFAULT_CAPTION_SINK = CaptionSink()


class VisualObserver(PollingLoop):
    _COMPENSATE_SLEEP = True

    def __init__(
        self,
        page: VisualPage,
        store: VisualStore,
        *,
        schedule: PollSchedule = DEFAULT_POLL_SCHEDULE,
        captions: CaptionSink = DEFAULT_CAPTION_SINK,
        sinks: ConsumerSink | None = None,
    ) -> None:
        super().__init__(
            poll_interval_seconds=schedule.interval_seconds,
            monotonic=schedule.monotonic,
            guard=create_poll_guard(sinks, source="browser", operation="visual_observer"),
        )
        self._page = page
        self._store = store
        self._journal = sinks.journal if sinks is not None else None
        self._captioner = captions.captioner
        self._on_caption = captions.on_caption
        self._hits = 0
        self._presenting = False
        self._eager: set[asyncio.Task[None]] = set()
        self.caption_failures = 0
        self.last_caption_error: str | None = None
        self.capture_failures = 0
        self.last_capture_error: str | None = None

    @property
    def presenting(self) -> bool:
        return self._presenting

    @override
    async def close(self) -> None:
        await super().close()
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
        try:
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                scan = await self._page.evaluate(VISUAL_SCAN_JS)
        except asyncio.CancelledError:
            raise
        except (PlaywrightError, TimeoutError) as error:
            self._record_capture_failure(type(error).__name__, stage="scan")
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

    @override
    async def _poll_once(self) -> None:
        self.polls += 1
        await self.poll()

    async def _caption_and_emit(self, frame: VisualFrame) -> None:
        captioner = self._captioner
        if captioner is None:
            return
        try:
            caption = await captioner.caption_frame(frame)
        except asyncio.CancelledError:
            raise
        except (OSError, ProviderFault, RuntimeError, FakeYouError) as error:
            self._record_caption_failure(type(error).__name__, stage="caption")
            return
        if caption is None:
            return
        callback = self._on_caption
        if callback is None:
            return
        try:
            callback(frame, caption)
        except (OSError, RuntimeError) as error:
            self._record_caption_failure(type(error).__name__, stage="emit")

    def _record_capture_failure(self, error_type: str, *, stage: str) -> None:
        self.capture_failures += 1
        self.last_capture_error = error_type
        if self._journal is not None:
            self._journal.record(
                "visual_capture_failed",
                source="browser",
                stage=stage,
                error_type=error_type,
            )

    def _record_caption_failure(self, error_type: str, *, stage: str) -> None:
        self.caption_failures += 1
        self.last_caption_error = error_type
        if self._journal is not None:
            self._journal.record(
                "visual_caption_failed",
                source="browser",
                stage=stage,
                error_type=error_type,
            )

    def _schedule_eager_caption(self, frame: VisualFrame) -> None:
        if self._captioner is None:
            return
        task = asyncio.create_task(self._caption_and_emit(frame))
        self._eager.add(task)
        task.add_done_callback(self._eager.discard)
        task.add_done_callback(self._eager_finished)

    def _eager_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        self._record_caption_failure(type(error).__name__, stage="eager")

    async def _capture(self, scan: Any) -> VisualFrame | None:
        started = self._monotonic()
        try:
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
                found = await self._page.evaluate(VISUAL_CONTENT_BOX_JS)
        except asyncio.CancelledError:
            raise
        except (PlaywrightError, TimeoutError):
            found = None
        box = _content_box(found)
        try:
            async with asyncio.timeout(POLL_TIMEOUT_MULTIPLIER * self._poll_interval):
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
        except (PlaywrightError, TimeoutError) as error:
            self._record_capture_failure(type(error).__name__, stage="screenshot")
            return None
        page_ms = _scan_page_ms(scan)
        frame, jpeg_bytes = await asyncio.to_thread(
            prepare_frame, bytes(raw), page_ms, self._store.directory
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
                visual_id=frame.frame_id,
                kind=VISUAL_KIND,
                chars=jpeg_bytes,
                duration_ms=(self._monotonic() - started) * 1000,
                capture=capture,
            )
        return frame
