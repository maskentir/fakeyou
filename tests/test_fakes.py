import os

import pytest

from fakeyou.settings import Settings


@pytest.mark.parametrize("prefix", ["FAKEYOU_", "fakeyou_", "FakeYou_"])
def test_settings_fixture_cleans_environment_and_uses_the_real_schema(monkeypatch, request, prefix):
    monkeypatch.setenv(f"{prefix}CHAT_MODEL", "external:model")
    monkeypatch.setenv(f"{prefix}TTS_PROVIDER", "invalid-provider")
    monkeypatch.setenv(f"{prefix}SESSION_DURATION_SECONDS", "invalid-seconds")

    settings = request.getfixturevalue("settings")

    assert isinstance(settings, Settings)
    assert settings.chat_model == "fake:model"
    assert settings.tts_provider == "openai"
    assert settings.session_duration_seconds == 7200.0
    assert not any(
        name.lower().startswith(Settings.model_config["env_prefix"].lower()) for name in os.environ
    )
    with pytest.raises(ValueError, match="no field"):
        settings.chat_modle = "typo:model"


@pytest.mark.parametrize(
    "method,args",
    [("search", ("agenda",)), ("add_episode", ()), ("retrieve_episodes", ())],
)
async def test_graphiti_double_rejects_unknown_library_arguments(graphiti, method, args):
    with pytest.raises(TypeError):
        await getattr(graphiti, method)(*args, typo_argument=True)
