from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from PIL import Image

from fakeyou.config.settings import PerceptionSettings
from fakeyou.visual.visual_caption import VisualCaptioner, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore
from tests._persona_support import profile
from tests._visual_fakes import caption_model

AT = "2026-10-16T00:00:00Z"


def _store_with_frame(tmp_path: Path, frame_id: str) -> tuple[VisualStore, VisualFrame]:
    shot = tmp_path / f"{frame_id}.jpg"
    Image.new("RGB", (64, 48), (20, 40, 80)).save(shot, format="JPEG", quality=60)
    frame = VisualFrame(
        frame_id=frame_id,
        captured_page_ms=1_000.0,
        captured_at=AT,
        sha256="ab" * 32,
        path=str(shot),
        thumb_path=str(tmp_path / f"{frame_id}.thumb.jpg"),
        dhash=0,
    )
    store = VisualStore(tmp_path / "visual")
    store.append(frame)
    return store, frame


def _empty_store(tmp_path: Path) -> VisualStore:
    return VisualStore(tmp_path / "visual")


class _ExhaustedCaptioner:
    async def caption_frame(self, frame: VisualFrame) -> None:
        del frame
        return None


def _captioner(text: str) -> VisualCaptioner:
    return VisualCaptioner(caption_model(text), settings=PerceptionSettings(enabled=True))


def test_visual_tool_needs_persona_and_visual_enabled() -> None:
    from fakeyou.visual.visual_tools import visual_tool_allowed

    assert visual_tool_allowed({"describe_frame"}, visual_enabled=True) is True
    assert visual_tool_allowed({"describe_frame"}, visual_enabled=False) is False
    assert visual_tool_allowed({"meet_send_chat"}, visual_enabled=True) is False
    assert visual_tool_allowed(set(), visual_enabled=False) is False


async def test_unknown_frame_is_named_error_not_caption(tmp_path: Path) -> None:
    from fakeyou.visual.visual_tools import create_visual_tools

    tool = create_visual_tools(_empty_store(tmp_path), _captioner("never used"))[0]
    payload = json.loads(await tool.ainvoke({"frame_id": "frame-404"}))
    assert payload["code"] == "unknown_frame"
    assert "never used" not in payload["message"]


async def test_exhausted_caption_budget_is_named_error(tmp_path: Path) -> None:
    from fakeyou.visual.visual_tools import create_visual_tools

    store, frame = _store_with_frame(tmp_path, "frame-7")
    tool = create_visual_tools(store, cast(Any, _ExhaustedCaptioner()))[0]
    payload = json.loads(await tool.ainvoke({"frame_id": frame.frame_id}))
    assert payload["code"] == "caption_unavailable"
    assert frame_ref(frame) in payload["message"]


async def test_caption_result_is_named_ok_with_frame_ref(tmp_path: Path) -> None:
    from fakeyou.visual.visual_tools import create_visual_tools

    store, frame = _store_with_frame(tmp_path, "frame-8")
    tool = create_visual_tools(store, _captioner("A bar chart."))[0]
    payload = json.loads(await tool.ainvoke({"frame_id": frame.frame_id}))
    assert payload["code"] == "ok"
    assert payload["message"] == f"A bar chart.\n{frame_ref(frame)}"


def test_persona_schema_accepts_describe_frame() -> None:
    persona = profile(allowed_tools=["describe_frame"])
    assert "describe_frame" in persona.allowed_tools
