from __future__ import annotations

import io
from typing import Any, override

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from PIL import Image

from fakeyou.visual.visual_observer import VISUAL_SCAN_JS


class FakeVisualModel(FakeMessagesListChatModel):
    seen: list[list[BaseMessage]] = []

    @override
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def caption_model(text: str, total_tokens: int | None = 1100) -> FakeVisualModel:
    usage: dict[str, Any] | None = None
    if total_tokens is not None:
        usage = {
            "input_tokens": total_tokens - 100,
            "output_tokens": 100,
            "total_tokens": total_tokens,
        }
    return FakeVisualModel(responses=[AIMessage(content=text, usage_metadata=usage)])


PRESENT_SCAN: dict[str, Any] = {
    "labels": [
        "Alex is presenting",
        "Presentation",
        "Unpin Alex's presentation from your main screen",
    ],
    "page_ms": 1700000000000.0,
}

CLIP_BOX: dict[str, float] = {"x": 173.0, "y": 65.0, "width": 592.0, "height": 385.0}


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
                result = self._scans.pop(0)
            else:
                result = self._scans[0]
            if isinstance(result, Exception):
                raise result
            return result
        if not self._boxes:
            raise AssertionError("box evaluate called more times than scripted")
        if len(self._boxes) > 1:
            result = self._boxes.pop(0)
        else:
            result = self._boxes[0]
        if isinstance(result, Exception):
            raise result
        return result

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.shot_kwargs.append(kwargs)
        if not self._shots:
            raise AssertionError("page screenshot called more times than scripted")
        result = self._shots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
