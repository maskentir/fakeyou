import os

import pytest

from fakeyou.settings import Settings


def test_settings_fixture_cleans_environment_and_uses_the_real_schema(monkeypatch, request):
    monkeypatch.setenv("FAKEYOU_CHAT_MODEL", "external:model")
    monkeypatch.setenv("FAKEYOU_TTS_PROVIDER", "invalid-provider")
    monkeypatch.setenv("FAKEYOU_SESSION_DURATION_SECONDS", "invalid-seconds")

    settings = request.getfixturevalue("settings")

    assert isinstance(settings, Settings)
    assert settings.chat_model == "fake:model"
    assert settings.tts_provider == "openai"
    assert settings.session_duration_seconds == 7200.0
    assert not any(name.startswith("FAKEYOU_") for name in os.environ)
    with pytest.raises(ValueError, match="no field"):
        settings.chat_modle = "typo:model"


@pytest.mark.parametrize(
    "method,args",
    [("search", ("agenda",)), ("add_episode", ()), ("retrieve_episodes", ())],
)
async def test_graphiti_double_rejects_unknown_library_arguments(graphiti, method, args):
    with pytest.raises(TypeError):
        await getattr(graphiti, method)(*args, typo_argument=True)
