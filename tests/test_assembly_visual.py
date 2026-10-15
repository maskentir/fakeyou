from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import fakeyou.orchestration.assembly as assembly_module
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import VISUAL_GUIDANCE
from fakeyou.conversation.session import SessionConfig
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.assembly import AgentWiring, LoopBehavior
from fakeyou.visual.visual_caption import VisualCaptioner
from fakeyou.visual.visual_store import VisualStore
from tests._conversation_fakes import ToolCallingFakeModel
from tests._memory_fakes import make_graphiti


def _wired_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"visual": settings.visual.model_copy(update={"enabled": True})}
    )


def _profile(settings: Settings) -> PersonaProfile:
    return PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Visual Twin",
        role="Project coordinator",
        allowed_tools=frozenset({"recall_meeting_context"}),
    )


def _config(profile: PersonaProfile) -> SessionConfig:
    return SessionConfig(
        owner_id=profile.owner_id,
        twin_id=profile.twin_id,
        meeting_id=SessionId("visual-wiring"),
        link="https://meet.google.com/abc-defg-hij",
        twin_name="Visual Twin",
    )


def _capture(created: dict[str, Any]) -> Any:
    def fake_agent(
        _model: Any, *, tools: Any = (), _checkpointer: Any = None, **kwargs: Any
    ) -> Any:
        created["tools"] = [tool.name for tool in tools]
        created["middleware"] = [type(item).__name__ for item in kwargs["middleware"]]
        created["persona"] = kwargs["persona"]
        return SimpleNamespace(name="agent")

    return fake_agent


async def test_assembly_registers_describe_frame_when_visual_wired(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    monkeypatch.setattr(assembly_module, "create_meeting_agent", _capture(created))
    wired = _wired_settings(settings)
    profile = _profile(settings)
    store = VisualStore(tmp_path / "visual")
    captioner = VisualCaptioner(ToolCallingFakeModel(responses=[], seen=[]))

    async with assembly_module.open_conversation(
        wired,
        _config(profile),
        agent=AgentWiring(
            profile=profile,
            graphiti=make_graphiti(),
            visual_store=store,
            visual_captioner=captioner,
        ),
        behavior=LoopBehavior(on_transition=lambda *_args: None),
    ):
        pass

    assert "describe_frame" in created["tools"]
    assert "VisualHydrateMiddleware" in created["middleware"]
    assert VISUAL_GUIDANCE in created["persona"]


async def test_assembly_registers_describe_frame_without_profile(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    monkeypatch.setattr(assembly_module, "create_meeting_agent", _capture(created))
    wired = _wired_settings(settings)
    store = VisualStore(tmp_path / "visual")
    captioner = VisualCaptioner(ToolCallingFakeModel(responses=[], seen=[]))

    async with assembly_module.open_conversation(
        wired,
        _config(_profile(settings)),
        agent=AgentWiring(visual_store=store, visual_captioner=captioner),
        behavior=LoopBehavior(on_transition=lambda *_args: None),
    ):
        pass

    assert created["tools"] == ["describe_frame"]
    assert "VisualHydrateMiddleware" in created["middleware"]
    assert VISUAL_GUIDANCE in created["persona"]


async def test_assembly_omits_visual_tooling_when_flag_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    created: dict[str, Any] = {}
    monkeypatch.setattr(assembly_module, "create_meeting_agent", _capture(created))
    assert settings.visual.enabled is False
    profile = _profile(settings)
    store = VisualStore(tmp_path / "visual")
    captioner = VisualCaptioner(
        ToolCallingFakeModel(responses=[AIMessage(content="unused")], seen=[])
    )

    async with assembly_module.open_conversation(
        settings,
        _config(profile),
        agent=AgentWiring(
            profile=profile,
            graphiti=make_graphiti(),
            visual_store=store,
            visual_captioner=captioner,
        ),
        behavior=LoopBehavior(on_transition=lambda *_args: None),
    ):
        pass

    assert "describe_frame" not in created["tools"]
    assert "VisualHydrateMiddleware" not in created["middleware"]
    assert VISUAL_GUIDANCE not in created["persona"]


def test_create_visual_captioner_returns_none_when_flag_off(settings: Settings) -> None:
    assert settings.visual.enabled is False
    assert assembly_module.create_visual_captioner(settings) is None


def test_create_visual_captioner_uses_visual_chat_model(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Settings] = []
    sentinel = ToolCallingFakeModel(responses=[], seen=[])

    def fake_factory(active: Settings) -> Any:
        seen.append(active)
        return sentinel

    monkeypatch.setattr(assembly_module, "create_visual_chat_model", fake_factory)
    wired = _wired_settings(settings)

    captioner = assembly_module.create_visual_captioner(wired)

    assert seen == [wired]
    assert isinstance(captioner, VisualCaptioner)
    assert captioner.spent_usd == 0.0
