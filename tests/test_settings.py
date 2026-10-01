import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from fakeyou.settings import Settings, require_secret


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("value", [None, SecretStr(""), SecretStr("  ")])
def test_missing_credentials_fail_before_starting_resources(value):
    with pytest.raises(ValueError, match="Set FAKEYOU_STT__API_KEY"):
        require_secret(value, "FAKEYOU_STT__API_KEY")


def test_meeting_settings_read_nested_environment(monkeypatch):
    monkeypatch.setenv("FAKEYOU_IDENTITY__TWIN_NAME", "Env Twin")
    monkeypatch.setenv("FAKEYOU_IDENTITY__OWNER_ID", "env-owner")
    monkeypatch.setenv("FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS", "45")
    settings = Settings()
    assert settings.identity.twin_name == "Env Twin"
    assert settings.identity.owner_id == "env-owner"
    assert settings.meeting.lobby_timeout_seconds == 45.0


def test_neo4j_password_has_no_default_secret():
    assert Settings().memory.store.password is None
    with pytest.raises(ValueError, match="FAKEYOU_MEMORY__STORE__PASSWORD"):
        require_secret(Settings().memory.store.password, "FAKEYOU_MEMORY__STORE__PASSWORD")


@pytest.mark.parametrize(
    "key", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_API_KEY", "CLOAKBROWSER_LICENSE_KEY"]
)
def test_secret_keys_ignore_bare_environment(monkeypatch, key):
    monkeypatch.setenv(key, "bare-secret")
    settings = Settings()
    assert settings.reply.api_key is None
    assert settings.tts.api_key is None
    assert settings.stt.api_key is None
    assert settings.memory.llm.api_key is None
    assert settings.memory.embedder.api_key is None
    assert settings.meeting.license_key is None


def test_secret_keys_read_explicit_role_environment(monkeypatch):
    monkeypatch.setenv("FAKEYOU_REPLY__PROVIDER", "openai")
    monkeypatch.setenv("FAKEYOU_REPLY__API_KEY", "reply-secret")
    monkeypatch.setenv("FAKEYOU_STT__API_KEY", "stt-secret")
    settings = Settings()
    assert settings.reply.api_key.get_secret_value() == "reply-secret"
    assert settings.stt.api_key.get_secret_value() == "stt-secret"
    assert settings.tts.api_key is None


def test_env_example_values_match_settings_defaults():
    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert Settings(_env_file=example).model_dump() == Settings(_env_file=None).model_dump()
