import os
from typing import Any
from unittest.mock import MagicMock

import pytest

from fakeyou.config.settings import Settings


@pytest.mark.parametrize("prefix", ["FAKEYOU_", "fakeyou_", "FakeYou_"])
def test_settings_fixture_cleans_environment_and_uses_the_real_schema(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, prefix: str
) -> None:
    monkeypatch.setenv(f"{prefix}CHAT_MODEL", "external:model")
    monkeypatch.setenv(f"{prefix}TTS_PROVIDER", "invalid-provider")
    monkeypatch.setenv(f"{prefix}SESSION_DURATION_SECONDS", "invalid-seconds")

    settings = request.getfixturevalue("settings")

    assert isinstance(settings, Settings)
    assert settings.reply.model == "model"
    assert settings.tts.provider == "openai"
    assert settings.meeting.session_duration_seconds == 7200.0
    assert not any(
        name.lower().startswith(Settings.model_config["env_prefix"].lower()) for name in os.environ
    )
    with pytest.raises(ValueError, match="no field"):
        settings.chat_modle = "typo:model"


@pytest.mark.parametrize(
    ("method", "args"),
    [("search", ("agenda",)), ("add_episode", ()), ("retrieve_episodes", ())],
)
async def test_graphiti_double_rejects_unknown_library_arguments(
    graphiti: MagicMock, method: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(TypeError):
        await getattr(graphiti, method)(*args, typo_argument=True)
