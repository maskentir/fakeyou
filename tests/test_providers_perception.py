from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import (
    GoogleReply,
    OpenAIReply,
    PerceptionInterpreterSettings,
    PerceptionSettings,
    Settings,
)
from fakeyou.orchestration import providers
from fakeyou.orchestration.providers import create_perception_chat_model

NO_ENV_FILE: dict[str, Any] = {"_env_file": None}

InitCalls = list[tuple[tuple[Any, ...], dict[str, Any]]]


def _capture_init(monkeypatch: pytest.MonkeyPatch) -> tuple[InitCalls, FakeMessagesListChatModel]:
    calls: InitCalls = []
    sentinel = FakeMessagesListChatModel(responses=[AIMessage(content="ok")])

    def fake_init(*args: Any, **kwargs: Any) -> FakeMessagesListChatModel:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(providers, "init_chat_model", fake_init)
    return calls, sentinel


def _perception(**interpreter: Any) -> PerceptionSettings:
    return PerceptionSettings(
        enabled=True, interpreter=PerceptionInterpreterSettings(**interpreter)
    )


def test_perception_role_routes_through_init_chat_model(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sentinel = _capture_init(monkeypatch)
    settings = Settings(perception=_perception(api_key=SecretStr("test-perception-key")))

    assert create_perception_chat_model(settings) is sentinel
    args, kwargs = calls[0]
    assert len(calls) == 1
    assert args[0] == "gemini-3.8-flash"
    assert kwargs["timeout"] == settings.perception.interpreter.timeout_seconds
    assert kwargs["model_provider"] == "google_genai"
    assert kwargs["api_key"] == "test-perception-key"
    assert kwargs["base_url"] == GoogleReply().resolved_base_url


def test_perception_role_ignores_the_reply_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sentinel = _capture_init(monkeypatch)
    settings = Settings(
        reply=GoogleReply(api_key=SecretStr("reply-key")),
        perception=_perception(
            provider="openai", model="gpt-4.1-mini", api_key=SecretStr("perception-key")
        ),
    )

    assert create_perception_chat_model(settings) is sentinel
    args, kwargs = calls[0]
    assert args[0] == "gpt-4.1-mini"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == OpenAIReply().resolved_base_url


def test_compatible_perception_routes_to_its_own_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, sentinel = _capture_init(monkeypatch)
    settings = Settings(
        reply=OpenAIReply(api_key=SecretStr("reply-key")),
        perception=_perception(
            provider="openai_compatible",
            model="qwen-3-vl",
            base_url="https://perception.example/v1",
            api_key=SecretStr("perception-key"),
        ),
    )

    assert create_perception_chat_model(settings) is sentinel
    args, kwargs = calls[0]
    assert args[0] == "qwen-3-vl"
    assert kwargs["model_provider"] == "openai"
    assert kwargs["base_url"] == "https://perception.example/v1"


def test_enabled_compatible_perception_without_base_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="FAKEYOU_PERCEPTION__INTERPRETER__BASE_URL"):
        _perception(provider="openai_compatible", api_key=SecretStr("perception-key"))


def test_compatible_perception_without_base_url_fails_fast_when_validation_is_bypassed() -> None:
    perception = PerceptionSettings.model_construct(
        enabled=True,
        interpreter=PerceptionInterpreterSettings(
            provider="openai_compatible", api_key=SecretStr("perception-key")
        ),
    )
    settings = Settings(reply=OpenAIReply(api_key=SecretStr("reply-key")))
    bypassed = settings.model_copy(update={"perception": perception})

    with pytest.raises(FakeYouError, match="FAKEYOU_PERCEPTION__INTERPRETER__BASE_URL") as caught:
        create_perception_chat_model(bypassed)
    assert caught.value.code is ErrorCode.CONFIG_PERCEPTION_BASE_URL_REQUIRED


def test_perception_role_never_borrows_the_reply_key() -> None:
    settings = Settings(
        reply=OpenAIReply(api_key=SecretStr("reply-key")),
        perception=PerceptionSettings(enabled=True),
    )

    with pytest.raises(FakeYouError, match="FAKEYOU_PERCEPTION__INTERPRETER__API_KEY") as caught:
        create_perception_chat_model(settings)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_perception_settings_defaults() -> None:
    settings = PerceptionSettings()

    assert settings.enabled is True
    assert (settings.screen, settings.chat, settings.signals, settings.cameras) == (
        True,
        True,
        True,
        True,
    )
    assert settings.interpreter.provider == "google"
    assert settings.interpreter.model == "gemini-3.8-flash"
    assert settings.interpreter.timeout_seconds == 30.0
    assert settings.budget_usd == 1.0
    assert settings.max_calls == 240
    assert settings.view_max_chars == 6000
    assert settings.retention_raw_days == 7
    assert settings.retention_text_days == 30
    assert settings.attention.enabled is True
    assert Settings(**NO_ENV_FILE).perception == settings
