import json
from collections.abc import Set as AbstractSet
from typing import Literal

from langchain_core.tools import BaseTool, tool

from fakeyou.visual.visual_caption import VisualCaptioner, frame_ref
from fakeyou.visual.visual_store import VisualStore

DESCRIBE_FRAME = "describe_frame"
VisualToolCode = Literal["ok", "unknown_frame", "caption_unavailable"]


def _render(code: VisualToolCode, message: str) -> str:
    return json.dumps({"code": code, "message": message}, ensure_ascii=False, separators=(",", ":"))


def visual_tool_allowed(allowed_tools: AbstractSet[str], *, visual_enabled: bool) -> bool:
    return visual_enabled and DESCRIBE_FRAME in allowed_tools


def create_visual_tools(store: VisualStore, captioner: VisualCaptioner) -> list[BaseTool]:
    @tool(description="Describe a captured meeting frame in detail by its frame id.")
    async def describe_frame(frame_id: str) -> str:
        frame = next((item for item in store.frames if item.frame_id == frame_id), None)
        if frame is None:
            return _render("unknown_frame", f"Unknown frame {frame_id}; no caption available.")
        caption = await captioner.caption_frame(frame)
        if caption is None:
            return _render(
                "caption_unavailable",
                f"Caption unavailable; visual budget exhausted. {frame_ref(frame)}",
            )
        return _render("ok", f"{caption.text}\n{frame_ref(frame)}")

    return [describe_frame]
