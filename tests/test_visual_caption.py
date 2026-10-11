import base64
import io
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from PIL import Image
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import GoogleReply, OpenAIReply, Settings, VisualSettings
from fakeyou.conversation.agent import create_visual_tools
from fakeyou.orchestration import providers
from fakeyou.orchestration.providers import create_visual_chat_model
from fakeyou.visual.visual_caption import (
    VISUAL_CAPTION_TTL_SECONDS,
    VISUAL_MAX_EDGE,
    VISUAL_USD_PER_MTOK,
    VisualCaptioner,
    estimate_tokens,
    frame_ref,
)
from fakeyou.visual.visual_store import VisualFrame, VisualStore, append_visual
from tests.fakes import FakeClock, FakeVisualModel, RecordingJournal, caption_model

AT = "2026-10-09T03:10:00+00:00"


def shot_file(directory: Path, name: str, width: int, height: int) -> Path:
    path = directory / name
    Image.new("RGB", (width, height), (20, 40, 80)).save(path, format="JPEG", quality=60)
    return path


def frame_for(directory: Path, frame_id: str, path: Path, sha: str, page_ms: float) -> VisualFrame:
    return VisualFrame(
        frame_id=frame_id,
        captured_page_ms=page_ms,
        captured_at=AT,
        sha256=sha,
        path=str(path),
        thumb_path=str(directory / f"{frame_id}.thumb.jpg"),
        dhash=0,
    )


async def test_caption_called_once_per_hash(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "shared.jpg", 64, 48)
    frames = [
        frame_for(tmp_path, f"000000000000{i}-aaaabbbb", path, "aa" * 32, 1700000000000.0 + i)
        for i in range(3)
    ]
    model = caption_model("A slide with quarterly numbers.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    captions = [await captioner.caption_frame(frame) for frame in frames]
    assert len(model.seen) == 1
    assert captions[0] is not None
    assert captions[0] == captions[1]
    assert captions[0] == captions[2]
    assert captions[0].visual_id == frames[0].frame_id
    assert captions[0].tokens == 1100
    assert captions[0].cost_usd == 1100 * VISUAL_USD_PER_MTOK / 1_000_000


async def test_caption_prompt_images_before_text(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "order.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000004-bbbbcccc", path, "bb" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    assert await captioner.caption_frame(frame) is not None
    assert len(model.seen) == 1
    [sent] = model.seen[0]
    assert isinstance(sent, HumanMessage)
    blocks = sent.content
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    first = blocks[0]
    second = blocks[1]
    assert isinstance(first, dict)
    assert isinstance(second, dict)
    assert first["type"] == "image"
    assert second["type"] == "text"
    assert first["mime_type"] == "image/jpeg"
    payload = base64.b64decode(first["base64"])
    with Image.open(io.BytesIO(payload)) as handle:
        assert handle.size == (64, 48)


async def test_describe_frame_returns_caption_plus_ref(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "tool.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000007-cccdddee", path, "ccdd" * 16, 1700000000000.0)
    store = VisualStore(tmp_path / "visual")
    append_visual(store, frame)
    model = caption_model("A bar chart.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    [describe] = create_visual_tools(store, captioner)
    assert describe.name == "describe_frame"
    output = await describe.ainvoke({"frame_id": frame.frame_id})
    assert "A bar chart." in output
    assert frame_ref(frame) in output
    assert output.endswith(f"frame:{frame.sha256[:8]}@{frame.captured_at}")


async def test_budget_stops_calls(tmp_path: Path) -> None:
    journal = RecordingJournal()
    first_path = shot_file(tmp_path, "first.jpg", 64, 48)
    second_path = shot_file(tmp_path, "second.jpg", 64, 48)
    first = frame_for(tmp_path, "0000000000008-eeeeffff", first_path, "11" * 32, 1700000000000.0)
    second = frame_for(tmp_path, "0000000000009-gggghhhh", second_path, "22" * 32, 1700000002000.0)
    model = caption_model("A slide.", total_tokens=1100)
    captioner = VisualCaptioner(
        model, settings=VisualSettings(enabled=True, budget_usd=0.001), journal=journal
    )
    assert await captioner.caption_frame(first) is not None
    assert await captioner.caption_frame(second) is None
    assert len(model.seen) == 1
    exhausted = journal.events("budget_exhausted")
    assert len(exhausted) == 1
    assert exhausted[0]["visual_id"] == second.frame_id
    assert captioner.spent_usd == 1100 * VISUAL_USD_PER_MTOK / 1_000_000


async def test_downscale_before_send(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "wide.jpg", 2000, 1000)
    frame = frame_for(tmp_path, "0000000000010-iiiijjjj", path, "33" * 32, 1700000000000.0)
    model = caption_model("A wide slide.", total_tokens=None)
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    caption = await captioner.caption_frame(frame)
    assert caption is not None
    [sent] = model.seen[0]
    blocks = sent.content
    assert isinstance(blocks, list)
    first = blocks[0]
    assert isinstance(first, dict)
    payload = base64.b64decode(first["base64"])
    with Image.open(io.BytesIO(payload)) as handle:
        assert max(handle.size) == VISUAL_MAX_EDGE
        assert handle.size == (1568, 784)
    with Image.open(path) as handle:
        assert handle.size == (2000, 1000)
    assert caption.tokens == estimate_tokens(1568, 784)
    assert caption.tokens == 56 * 28


async def test_cache_expires_after_ttl(tmp_path: Path) -> None:
    clock = FakeClock(now=1000.0)
    path = shot_file(tmp_path, "ttl.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000011-kkkkllll", path, "44" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(
        model, settings=VisualSettings(enabled=True), monotonic=clock.monotonic
    )
    assert await captioner.caption_frame(frame) is not None
    assert await captioner.caption_frame(frame) is not None
    assert len(model.seen) == 1
    clock.now += VISUAL_CAPTION_TTL_SECONDS + 1.0
    assert await captioner.caption_frame(frame) is not None
    assert len(model.seen) == 2


def test_visual_role_routes_through_init_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sentinel = FakeVisualModel(responses=[AIMessage(content="ok")])

    def fake_init(*args: Any, **kwargs: Any) -> FakeVisualModel:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    settings = Settings(visual=VisualSettings(api_key=SecretStr("test-visual-key")))
    assert settings.visual.provider == "google"
    assert create_visual_chat_model(settings) is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == settings.visual.model
    assert kwargs["timeout"] == settings.visual.timeout_seconds
    assert kwargs["model_provider"] == "google_genai"
    assert kwargs["api_key"] == "test-visual-key"
    assert kwargs["base_url"] == GoogleReply().resolved_base_url


def test_visual_role_ignores_reply_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sentinel = FakeVisualModel(responses=[AIMessage(content="ok")])

    def fake_init(*args: Any, **kwargs: Any) -> FakeVisualModel:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    settings = Settings(
        reply=GoogleReply(api_key=SecretStr("reply-key")),
        visual=VisualSettings(
            provider="openai", model="gpt-4.1-mini", api_key=SecretStr("visual-key")
        ),
    )
    assert create_visual_chat_model(settings) is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "gpt-4.1-mini"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == OpenAIReply().resolved_base_url


def test_visual_compatible_routes_to_visual_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    sentinel = FakeVisualModel(responses=[AIMessage(content="ok")])

    def fake_init(*args: Any, **kwargs: Any) -> FakeVisualModel:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    settings = Settings(
        reply=OpenAIReply(api_key=SecretStr("reply-key")),
        visual=VisualSettings(
            provider="openai_compatible",
            model="qwen-3-vl",
            base_url="https://visual.example/v1",
            api_key=SecretStr("visual-key"),
        ),
    )
    assert create_visual_chat_model(settings) is sentinel
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "qwen-3-vl"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == "https://visual.example/v1"


def test_visual_compatible_without_base_url_fails_fast() -> None:
    settings = Settings(
        reply=OpenAIReply(api_key=SecretStr("reply-key")),
        visual=VisualSettings(
            provider="openai_compatible", model="qwen-3-vl", api_key=SecretStr("visual-key")
        ),
    )
    with pytest.raises(FakeYouError, match="FAKEYOU_VISUAL__BASE_URL") as caught:
        create_visual_chat_model(settings)
    assert caught.value.code is ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED


def test_visual_role_never_borrows_the_reply_key() -> None:
    settings = Settings(
        reply=OpenAIReply(api_key=SecretStr("reply-key")), visual=VisualSettings(enabled=True)
    )
    with pytest.raises(FakeYouError, match="FAKEYOU_VISUAL__API_KEY") as caught:
        create_visual_chat_model(settings)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_visual_settings_defaults() -> None:
    settings = VisualSettings()
    assert settings.enabled is False
    assert settings.provider == "google"
    assert settings.model == "gemini-3.8-flash"
    assert settings.timeout_seconds == 30.0
    assert settings.budget_usd == 0.5
    assert settings.max_calls == 120
    assert settings.max_images == 2
    assert settings.retention_raw_days == 7
    assert settings.retention_text_days == 30
    assert Settings().visual == settings


async def test_call_cap_stops_captions(tmp_path: Path) -> None:
    journal = RecordingJournal()
    first_path = shot_file(tmp_path, "cap-first.jpg", 64, 48)
    second_path = shot_file(tmp_path, "cap-second.jpg", 64, 48)
    first = frame_for(tmp_path, "0000000000012-mmmmnnnn", first_path, "55" * 32, 1700000000000.0)
    second = frame_for(tmp_path, "0000000000013-oooopppp", second_path, "66" * 32, 1700000002000.0)
    model = caption_model("A slide.", total_tokens=1100)
    captioner = VisualCaptioner(
        model, settings=VisualSettings(enabled=True, max_calls=1), journal=journal
    )
    assert await captioner.caption_frame(first) is not None
    assert await captioner.caption_frame(second) is None
    assert len(model.seen) == 1
    assert len(journal.events("budget_exhausted")) == 1


async def test_describe_frame_unknown_frame(tmp_path: Path) -> None:
    store = VisualStore(tmp_path / "visual")
    model = caption_model("A slide.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    [describe] = create_visual_tools(store, captioner)
    output = await describe.ainvoke({"frame_id": "missing-frame"})
    assert "missing-frame" in output
    assert len(model.seen) == 0


async def test_cached_caption_miss_skips_network(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "miss.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000014-qqqqrrrr", path, "77" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))

    assert captioner.cached_caption(frame) is None
    assert len(model.seen) == 0


async def test_cached_caption_hit_returns_stored_caption(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "hit.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000015-sssstttt", path, "88" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(model, settings=VisualSettings(enabled=True))
    stored = await captioner.caption_frame(frame)

    assert stored is not None
    assert captioner.cached_caption(frame) == stored
    assert len(model.seen) == 1


async def test_cached_caption_expires_after_ttl(tmp_path: Path) -> None:
    clock = FakeClock(now=1000.0)
    path = shot_file(tmp_path, "expired.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000016-uuuuvvvv", path, "99" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(
        model, settings=VisualSettings(enabled=True), monotonic=clock.monotonic
    )
    assert await captioner.caption_frame(frame) is not None
    assert captioner.cached_caption(frame) is not None
    clock.now += VISUAL_CAPTION_TTL_SECONDS + 1.0

    assert captioner.cached_caption(frame) is None
    assert len(model.seen) == 1


async def test_cached_caption_disabled_returns_none(tmp_path: Path) -> None:
    path = shot_file(tmp_path, "off.jpg", 64, 48)
    frame = frame_for(tmp_path, "0000000000017-wwwwxxxx", path, "a0" * 32, 1700000000000.0)
    model = caption_model("A slide.")
    captioner = VisualCaptioner(model, settings=VisualSettings())

    assert captioner.cached_caption(frame) is None
    assert len(model.seen) == 0
