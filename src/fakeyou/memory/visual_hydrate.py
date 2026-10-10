import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from fakeyou.foundation.transcript import visual_refs_of
from fakeyou.memory.memory import VisualCaptionResolver, current_speech
from fakeyou.observability.journal import Journal
from fakeyou.visual.visual_caption import VisualCaptioner, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore

VISUAL_HYDRATE_MAX_IMAGES = 2
VISUAL_EVIDENCE_VERSION = "v1"
VISUAL_EVIDENCE_MIME = "image/jpeg"
VISUAL_CLARIFY_AMBIGUOUS = "I can see more than one shared slide. Which one do you mean?"
VISUAL_CLARIFY_MISSING = "I cannot see a shared slide right now. Which slide do you mean?"


def format_visual_line(ref: str, text: str | None) -> str:
    if text:
        return f"[visual {ref}: {text}]"
    return f"[visual {ref}]"


def visual_clarification(candidate_count: int) -> str | None:
    if candidate_count > 1:
        return VISUAL_CLARIFY_AMBIGUOUS
    if candidate_count == 0:
        return VISUAL_CLARIFY_MISSING
    return None


def _without_echo_images(message: AIMessage) -> AIMessage | None:
    if not isinstance(message.content, list):
        return None
    kept = [
        block
        for block in message.content
        if not (isinstance(block, dict) and block.get("type") in ("image", "image_url"))
    ]
    if len(kept) == len(message.content):
        return None
    return message.model_copy(update={"content": kept or ""})


class VisualHydrateMiddleware(AgentMiddleware[Any, Any, Any]):
    def __init__(
        self,
        store: VisualStore,
        captioner: VisualCaptioner,
        *,
        max_images: int = VISUAL_HYDRATE_MAX_IMAGES,
        journal: Journal | None = None,
    ) -> None:
        self._store = store
        self._captioner = captioner
        self._max_images = max_images
        self._journal = journal

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        _, speech = current_speech(request.messages)
        frames = self._resolve(visual_refs_of(speech[-1])) if speech else []
        if not frames:
            return self._strip_response(await handler(request))
        hydrated = await self._hydrate(speech[-1], frames)
        messages = [hydrated if message is speech[-1] else message for message in request.messages]
        return self._strip_response(await handler(request.override(messages=messages)))

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        tagged = [
            message
            for message in messages
            if isinstance(message, HumanMessage) and visual_refs_of(message)
        ]
        if len(tagged) < 2:
            return None
        trimmed: list[HumanMessage] = []
        for message in tagged[:-1]:
            trimmed.append(await self._trim(message))
        return {"messages": trimmed}

    def after_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        latest = messages[-1]
        if not isinstance(latest, AIMessage):
            return None
        stripped = _without_echo_images(latest)
        if stripped is None:
            return None
        return {"messages": [stripped]}

    def _strip_response(self, response: ModelResponse[Any]) -> ModelResponse[Any]:
        cleaned: list[BaseMessage] = []
        changed = False
        for message in response.result:
            stripped = _without_echo_images(message) if isinstance(message, AIMessage) else None
            if stripped is None:
                cleaned.append(message)
            else:
                cleaned.append(stripped)
                changed = True
        if not changed:
            return response
        return replace(response, result=cleaned)

    def _resolve(self, refs: list[str]) -> list[VisualFrame]:
        by_id = {frame.frame_id: frame for frame in self._store.frames}
        return [by_id[ref] for ref in refs if ref in by_id]

    def _caption_line(self, frame: VisualFrame) -> str:
        caption = self._captioner.cached_caption(frame)
        return format_visual_line(frame_ref(frame), caption.text if caption else None)

    async def _hydrate(self, message: HumanMessage, frames: list[VisualFrame]) -> HumanMessage:
        start = max(0, len(frames) - self._max_images) if self._max_images > 0 else len(frames)
        blocks: list[dict[str, str]] = []
        hydrated: list[str] = []
        for frame in frames[start:]:
            try:
                data = await asyncio.to_thread(Path(frame.path).read_bytes)
            except OSError:
                continue
            blocks.append(
                {
                    "type": "image",
                    "base64": base64.b64encode(data).decode("ascii"),
                    "mime_type": VISUAL_EVIDENCE_MIME,
                }
            )
            hydrated.append(frame.frame_id)
        lines = [format_visual_line(frame_ref(frame), None) for frame in frames]
        parts = [message.text, *lines] if message.text else lines
        if self._journal is not None and hydrated:
            self._journal.record(
                "visual_bound", source="agent", visual_ids=hydrated, images=len(blocks)
            )
        return message.model_copy(
            update={
                "content": [*blocks, {"type": "text", "text": "\n".join(parts)}],
                "additional_kwargs": {
                    **message.additional_kwargs,
                    "visual_evidence": {
                        "version": VISUAL_EVIDENCE_VERSION,
                        "visual_ids": hydrated,
                        "images": len(blocks),
                    },
                },
            }
        )

    async def _trim(self, message: HumanMessage) -> HumanMessage:
        frames = self._resolve(visual_refs_of(message))
        lines = [self._caption_line(frame) for frame in frames]
        event = dict(message.additional_kwargs["meeting_event"])
        event["visual_refs"] = []
        parts = [message.text, *lines] if message.text else lines
        return message.model_copy(
            update={
                "content": "\n".join(parts),
                "additional_kwargs": {**message.additional_kwargs, "meeting_event": event},
            }
        )


def create_visual_hydrate_middleware(
    store: VisualStore,
    captioner: VisualCaptioner,
    *,
    max_images: int = VISUAL_HYDRATE_MAX_IMAGES,
    journal: Journal | None = None,
) -> AgentMiddleware[Any, Any, Any]:
    return VisualHydrateMiddleware(store, captioner, max_images=max_images, journal=journal)


def create_visual_resolver(store: VisualStore, captioner: VisualCaptioner) -> VisualCaptionResolver:
    async def resolve(ref: str) -> str | None:
        frame = next((item for item in store.frames if item.frame_id == ref), None)
        if frame is None:
            return None
        caption = captioner.cached_caption(frame)
        text = caption.text if caption is not None else None
        return format_visual_line(frame_ref(frame), text)

    return resolve
