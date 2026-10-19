import json
import os

import pytest
from pydantic import SecretStr

from fakeyou.config.sanitize import (
    INVALID_ENDPOINT,
    REDACTED,
    is_credential_key,
    safe_endpoint,
    sanitized_settings_dump,
)
from fakeyou.config.settings import CompatibleReply, Settings
from tests._fakes_common import env_file_opt

MARKER = "synthetic-marker-9f3a"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize(
    "value",
    [
        f"https://user:{MARKER}@api.example.com/v1?key={MARKER}#frag",
        f"not a url :// {MARKER}",
        f"https://[::1:badport/{MARKER}",
        "",
        None,
        42,
    ],
)
def test_safe_endpoint_never_returns_unparseable_input(value: object) -> None:
    result = safe_endpoint(value)
    assert MARKER not in result
    assert result == INVALID_ENDPOINT or result == ("https://api.example.com/v1")


@pytest.mark.parametrize(
    "value",
    [
        f"https://api.example.com:{MARKER}/v1",
        "https://api.example.com:99999/v1",
        "https:///no-host",
        "   ",
        b"https://api.example.com/v1",
        ["https://api.example.com/v1"],
    ],
)
def test_safe_endpoint_rejects_invalid_port_host_and_type(value: object) -> None:
    result = safe_endpoint(value)
    assert result == INVALID_ENDPOINT
    assert MARKER not in result


def test_safe_endpoint_strips_userinfo_query_and_fragment() -> None:
    assert safe_endpoint("wss://api.meta.ai:8443/v1/asr") == "wss://api.meta.ai:8443/v1/asr"
    assert safe_endpoint(f"https://user:{MARKER}@api.example.com/v1") == (
        "https://api.example.com/v1"
    )


def test_safe_endpoint_brackets_ipv6_host() -> None:
    assert safe_endpoint("https://[2001:db8::1]:8443/v1") == "https://[2001:db8::1]:8443/v1"


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "provider_access_key_value",
        "database_password_text",
        "clientSecret",
        "authorization_header",
        "owner_token",
        "license_key",
    ],
)
def test_credential_keys_are_recognized_by_meaning(name: str) -> None:
    assert is_credential_key(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "database_passphrase_text",
        "pass_word",
        "pass_phrase",
        "service_bearer_value",
        "refreshTokenValue",
        "x-api-key",
        "session_cookie_value",
        "account_pwd",
        "oauth2_client_assertion_value",
        "rotated_upstream_signing_credential_blob",
    ],
)
def test_credential_synonyms_and_long_names_are_recognized(name: str) -> None:
    assert is_credential_key(name) is True


@pytest.mark.parametrize("name", ["model", "timeout_seconds", "host", "language"])
def test_plain_keys_are_not_credentials(name: str) -> None:
    assert is_credential_key(name) is False


@pytest.mark.parametrize("name", ["author", "keyboard_layout", "tokenizer_model", "secretary_name"])
def test_keys_sharing_only_letters_with_credentials_are_plain(name: str) -> None:
    assert is_credential_key(name) is False


def test_sanitized_dump_hides_every_marker() -> None:
    settings = Settings(
        **env_file_opt(None),
        reply=CompatibleReply(
            base_url="https://api.example.com/v1",
            api_key=SecretStr(MARKER),
            extra_body={"provider_access_key_value": MARKER, "nested": {"note": MARKER}},
        ),
    )
    settings.api.admin_token = SecretStr(MARKER)
    settings.transport.redis.url = SecretStr(f"redis://:{MARKER}@localhost:6379/0")
    rendered = json.dumps(sanitized_settings_dump(settings))
    assert MARKER not in rendered
    assert REDACTED in rendered


def test_sanitized_dump_never_passes_through_unparseable_url() -> None:
    reply = CompatibleReply(base_url="https://api.example.com/v1", api_key=SecretStr(MARKER))
    settings = Settings(**env_file_opt(None), reply=reply)
    reply.base_url = f"not a url :// {MARKER}"
    dumped = sanitized_settings_dump(settings)
    rendered = json.dumps(dumped)
    assert MARKER not in rendered
    assert dumped["reply"]["base_url"] == INVALID_ENDPOINT


def test_sanitized_dump_keeps_masked_secret_url_masked() -> None:
    settings = Settings(**env_file_opt(None))
    settings.database.url = SecretStr(f"postgresql://user:{MARKER}@localhost:5432/fakeyou")
    dumped = sanitized_settings_dump(settings)
    assert dumped["database"]["url"] == REDACTED
    assert MARKER not in json.dumps(dumped)


def test_sanitized_dump_keeps_plain_values_and_sanitizes_endpoint() -> None:
    settings = Settings(
        **env_file_opt(None),
        reply=CompatibleReply(
            base_url=f"https://user:{MARKER}@api.example.com/v1?key={MARKER}",
            model="plain-model-name",
        ),
    )
    dumped = sanitized_settings_dump(settings)
    assert dumped["reply"]["base_url"] == "https://api.example.com/v1"
    assert dumped["reply"]["model"] == "plain-model-name"
    assert dumped["reply"]["timeout_seconds"] == 15.0
    assert MARKER not in json.dumps(dumped)
