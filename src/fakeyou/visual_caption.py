import asyncio
import base64
import io
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from PIL import Image

from fakeyou.journal import Journal
from fakeyou.settings import VisualSettings
from fakeyou.visual_store import VisualFrame

VISUAL_MAX_EDGE = 1568
VISUAL_TILE_EDGE = 28
VISUAL_CAPTION_TTL_SECONDS = 1800.0
VISUAL_USD_PER_MTOK = 1.0
VISUAL_CAPTION_JPEG_QUALITY = 60
VISUAL_CAPTION_PROMPT = "Describe the visible slide or screen content in two short sentences."
VISUAL_CAPTION_MIME = "image/jpeg"


@dataclass(frozen=True, slots=True)
class VisualCaption:
    visual_id: str
    text: str
    tokens: int
    cost_usd: float


def estimate_tokens(width: int, height: int) -> int:
    return math.ceil(width / VISUAL_TILE_EDGE) * math.ceil(height / VISUAL_TILE_EDGE)


def frame_ref(frame: VisualFrame) -> str:
    return f"frame:{frame.sha256[:8]}@{frame.captured_at}"


def _payload_bytes(data: bytes) -> tuple[bytes, int, int]:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    width, height = image.size
    edge = max(width, height)
    if edge > VISUAL_MAX_EDGE:
        scale = VISUAL_MAX_EDGE / edge
        width = max(1, round(width * scale))
        height = max(1, round(height * scale))
        image = image.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=VISUAL_CAPTION_JPEG_QUALITY)
    return buffer.getvalue(), image.width, image.height


def _usage_total(usage: Mapping[str, Any] | None, estimate: int) -> int:
    if isinstance(usage, Mapping):
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            return total
    return estimate


class VisualCaptioner:
    def __init__(
        self,
        model: BaseChatModel,
        *,
        settings: VisualSettings | None = None,
        journal: Journal | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._settings = settings if settings is not None else VisualSettings()
        self._journal = journal
        self._monotonic = monotonic
        self._cache: dict[str, tuple[VisualCaption, float]] = {}
        self._spent_usd = 0.0
        self._calls = 0

    @property
    def spent_usd(self) -> float:
        return self._spent_usd

    async def caption_frame(self, frame: VisualFrame) -> VisualCaption | None:
        if not self._settings.enabled:
            return None
        now = self._monotonic()
        cached = self._cache.get(frame.sha256)
        if cached is not None:
            caption, stored_at = cached
            if now - stored_at <= VISUAL_CAPTION_TTL_SECONDS:
                return caption
            del self._cache[frame.sha256]
        data = await asyncio.to_thread(Path(frame.path).read_bytes)
        payload, width, height = _payload_bytes(data)
        estimate = estimate_tokens(width, height)
        if self._spent_usd + estimate * VISUAL_USD_PER_MTOK / 1_000_000 > self._settings.budget_usd:
            if self._journal is not None:
                self._journal.record(
                    "budget_exhausted",
                    source="llm",
                    visual_id=frame.frame_id,
                    spent_usd=self._spent_usd,
                    budget_usd=self._settings.budget_usd,
                )
            return None
        if self._calls >= self._settings.max_calls:
            if self._journal is not None:
                self._journal.record(
                    "budget_exhausted",
                    source="llm",
                    visual_id=frame.frame_id,
                    spent_usd=self._spent_usd,
                    budget_usd=self._settings.budget_usd,
                )
            return None
        started = self._monotonic()
        message = HumanMessage(
            content=[
                {
                    "type": "image",
                    "base64": base64.b64encode(payload).decode("ascii"),
                    "mime_type": VISUAL_CAPTION_MIME,
                },
                {"type": "text", "text": VISUAL_CAPTION_PROMPT},
            ]
        )
        response = await self._model.ainvoke([message])
        tokens = _usage_total(response.usage_metadata, estimate)
        cost = tokens * VISUAL_USD_PER_MTOK / 1_000_000
        caption = VisualCaption(
            visual_id=frame.frame_id,
            text=response.text,
            tokens=tokens,
            cost_usd=cost,
        )
        self._spent_usd += cost
        self._calls += 1
        self._cache[frame.sha256] = (caption, self._monotonic())
        if self._journal is not None:
            self._journal.record(
                "visual_captioned",
                source="llm",
                visual_id=frame.frame_id,
                tokens=tokens,
                cost_usd=cost,
                duration_ms=(self._monotonic() - started) * 1000,
            )
        return caption
