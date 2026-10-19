from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, override

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, convert_to_messages
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from PIL import Image, ImageDraw, ImageFont

from fakeyou.perception.frames import keyframe_from_jpeg
from fakeyou.perception.percepts import Keyframe, ScreenFigure, ScreenReading
from fakeyou.perception.timeline import MeetingTimeline

START = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def usage(total_tokens: int) -> UsageMetadata:
    return UsageMetadata(
        input_tokens=total_tokens - 100, output_tokens=100, total_tokens=total_tokens
    )


def slide_jpeg(
    title: str,
    lines: Sequence[str] = (),
    *,
    size: tuple[int, int] = (1280, 720),
    background: tuple[int, int, int] = (18, 32, 72),
    quality: int = 90,
) -> bytes:
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    scale = size[0] / 1280
    title_font = ImageFont.load_default(size=round(64 * scale))
    body_font = ImageFont.load_default(size=round(40 * scale))
    draw.text((round(80 * scale), round(70 * scale)), title, fill="white", font=title_font)
    for index, line in enumerate(lines):
        position = (round(100 * scale), round((220 + index * 80) * scale))
        draw.text(position, line, fill=(255, 196, 64), font=body_font)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def content_block(message: BaseMessage, index: int) -> dict[str, Any]:
    assert isinstance(message.content, list)
    block = message.content[index]
    assert isinstance(block, dict)
    return block


def keyframe(data: bytes, *, seconds: float = 0.0) -> Keyframe:
    return keyframe_from_jpeg(data, captured_at=START + timedelta(seconds=seconds))


def reading(
    title: str,
    *,
    text: str = "",
    kind: str = "slide",
    figures: Sequence[tuple[str, str]] = (),
    key_points: Sequence[str] = (),
) -> ScreenReading:
    return ScreenReading.model_validate(
        {
            "content_kind": kind,
            "title": title,
            "text": text or title,
            "key_points": list(key_points),
            "figures": [ScreenFigure(label=label, value=value) for label, value in figures],
        }
    )


def _messages_of(model_input: LanguageModelInput) -> list[BaseMessage]:
    if isinstance(model_input, list):
        return convert_to_messages(model_input)
    return []


class FakeVisionModel(FakeMessagesListChatModel):
    readings: list[ScreenReading | None] = []
    reading_tokens: int = 1500
    seen: list[list[BaseMessage]] = []
    structured_seen: list[list[BaseMessage]] = []

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

    @override
    def with_structured_output(
        self, schema: dict[str, Any] | type, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, dict[str, Any] | Any]:
        def respond(model_input: LanguageModelInput) -> dict[str, Any]:
            self.structured_seen.append(_messages_of(model_input))
            parsed = self.readings.pop(0) if self.readings else None
            raw = AIMessage(content="", usage_metadata=usage(self.reading_tokens))
            return {"raw": raw, "parsed": parsed, "parsing_error": None}

        return RunnableLambda(respond)


def vision_model(
    readings: Sequence[ScreenReading | None] = (),
    answers: Sequence[str] = (),
    *,
    reading_tokens: int = 1500,
) -> FakeVisionModel:
    return FakeVisionModel(
        responses=[AIMessage(content=answer, usage_metadata=usage(900)) for answer in answers]
        or [AIMessage(content="unused")],
        readings=list(readings),
        reading_tokens=reading_tokens,
        seen=[],
        structured_seen=[],
    )


class FailingVisionModel(FakeVisionModel):
    error_factory: Callable[[], Exception] = lambda: RuntimeError("provider down")

    @override
    def with_structured_output(
        self, schema: dict[str, Any] | type, *, include_raw: bool = False, **kwargs: Any
    ) -> Runnable[LanguageModelInput, dict[str, Any] | Any]:
        def fail(model_input: LanguageModelInput) -> dict[str, Any]:
            self.structured_seen.append(_messages_of(model_input))
            raise self.error_factory()

        return RunnableLambda(fail)


def shown_timeline(
    *slides: tuple[str, Sequence[str]], seconds_apart: float = 30.0
) -> MeetingTimeline:
    timeline = MeetingTimeline()
    presenter = timeline.participant("device-ayu", "Ayu")
    for index, (title, lines) in enumerate(slides):
        data = slide_jpeg(title, lines)
        timeline.show_screen(keyframe(data, seconds=index * seconds_apart), presenter, data=data)
    return timeline
