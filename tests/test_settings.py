from pathlib import Path

import pytest
from pydantic import SecretStr

from fakeyou.settings import Settings, require_secret


def _revealed(value: SecretStr | str | None) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


@pytest.mark.parametrize("value", [None, SecretStr(""), SecretStr("  ")])
def test_missing_credentials_fail_before_starting_resources(value: SecretStr | None) -> None:
    with pytest.raises(ValueError, match="Set FAKEYOU_MODEL_API_KEY"):
        require_secret(value, "FAKEYOU_MODEL_API_KEY")


def test_meeting_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_TWIN_NAME", "Env Twin")
    monkeypatch.setenv("FAKEYOU_OWNER_ID", "env-owner")
    monkeypatch.setenv("FAKEYOU_LOBBY_TIMEOUT_SECONDS", "45")

    settings = Settings()

    assert settings.twin_name == "Env Twin"
    assert settings.owner_id == "env-owner"
    assert settings.lobby_timeout_seconds == 45.0


def test_neo4j_password_has_no_default_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKEYOU_NEO4J_PASSWORD", raising=False)
    assert Settings().neo4j_password is None


def test_neo4j_password_missing_fails_before_starting_resources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKEYOU_NEO4J_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="Set FAKEYOU_NEO4J_PASSWORD"):
        require_secret(Settings().neo4j_password, "FAKEYOU_NEO4J_PASSWORD")


@pytest.mark.parametrize(
    ("bare_key", "attribute"),
    [
        ("OPENAI_API_KEY", "openai_api_key"),
        ("MODEL_API_KEY", "model_api_key"),
        ("CLOAKBROWSER_LICENSE_KEY", "cloakbrowser_license_key"),
    ],
)
def test_secret_keys_ignore_bare_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_key: str, attribute: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(f"FAKEYOU_{bare_key}", raising=False)
    monkeypatch.setenv(bare_key, "bare-secret")
    assert getattr(Settings(), attribute) is None


@pytest.mark.parametrize(
    ("bare_key", "attribute"),
    [
        ("OPENAI_API_KEY", "openai_api_key"),
        ("MODEL_API_KEY", "model_api_key"),
        ("CLOAKBROWSER_LICENSE_KEY", "cloakbrowser_license_key"),
    ],
)
def test_secret_keys_read_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bare_key: str, attribute: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(bare_key, raising=False)
    monkeypatch.setenv(f"FAKEYOU_{bare_key}", "prefixed-secret")
    assert _revealed(getattr(Settings(), attribute)) == "prefixed-secret"
