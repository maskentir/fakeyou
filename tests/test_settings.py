import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import (
    DEFAULTS,
    IdentitySettings,
    MeetingSettings,
    Settings,
    STTSettings,
    require_secret,
)
from tests._fakes_common import env_file_opt


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("value", [None, SecretStr(""), SecretStr("  ")])
def test_missing_credentials_fail_before_starting_resources(value: SecretStr | None) -> None:
    with pytest.raises(FakeYouError, match="Set FAKEYOU_STT__API_KEY") as error:
        require_secret(value, ConfigKey.STT_API_KEY)
    assert error.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_meeting_settings_read_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_IDENTITY__TWIN_NAME", "Env Twin")
    monkeypatch.setenv("FAKEYOU_IDENTITY__OWNER_ID", "env-owner")
    monkeypatch.setenv("FAKEYOU_MEETING__LOBBY_TIMEOUT_SECONDS", "45")
    settings = Settings()
    assert settings.identity.twin_name == "Env Twin"
    assert settings.identity.owner_id == "env-owner"
    assert settings.meeting.lobby_timeout_seconds == 45.0


def test_neo4j_password_has_no_default_secret() -> None:
    assert Settings().memory.store.password is None
    with pytest.raises(FakeYouError, match="FAKEYOU_MEMORY__STORE__PASSWORD") as error:
        require_secret(Settings().memory.store.password, ConfigKey.MEMORY_STORE_PASSWORD)
    assert error.value.code is ErrorCode.CONFIG_SECRET_MISSING


@pytest.mark.parametrize(
    "key", ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "MODEL_API_KEY", "CLOAKBROWSER_LICENSE_KEY"]
)
def test_secret_keys_ignore_bare_environment(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    monkeypatch.setenv(key, "bare-secret")
    settings = Settings()
    assert settings.reply.api_key is None
    assert settings.tts.api_key is None
    assert settings.stt.api_key is None
    assert settings.memory.llm.api_key is None
    assert settings.memory.embedder.api_key is None
    assert settings.browser.license_key is None


def test_secret_keys_read_explicit_role_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_REPLY__PROVIDER", "openai")
    monkeypatch.setenv("FAKEYOU_REPLY__API_KEY", "reply-secret")
    monkeypatch.setenv("FAKEYOU_STT__API_KEY", "stt-secret")
    settings = Settings()
    assert settings.reply.api_key is not None
    assert settings.reply.api_key.get_secret_value() == "reply-secret"
    assert settings.stt.api_key is not None
    assert settings.stt.api_key.get_secret_value() == "stt-secret"
    assert settings.tts.api_key is None


def test_env_example_values_match_settings_defaults() -> None:
    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert (
        Settings(**env_file_opt(example)).model_dump()
        == Settings(**env_file_opt(None)).model_dump()
    )


def test_conversation_sentence_streaming_defaults_on() -> None:
    assert Settings().conversation.sentence_streaming is True


def test_conversation_sentence_streaming_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_CONVERSATION__SENTENCE_STREAMING", "true")
    assert Settings().conversation.sentence_streaming is True


def test_importing_fakeyou_disables_implicit_dotenv_loading(tmp_path: Path) -> None:
    env_file = tmp_path / "leak.env"
    env_file.write_text("FAKEYOU_LEAK_PROBE=leaked\n")
    script = (
        "import os\n"
        "import fakeyou.memory\n"
        "from dotenv import load_dotenv\n"
        f"load_dotenv({str(env_file)!r})\n"
        "print(os.environ.get('FAKEYOU_LEAK_PROBE'))\n"
    )
    environment = {
        key: value for key, value in os.environ.items() if key != "PYTHON_DOTENV_DISABLED"
    }
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment, capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines()[-1] == "None"


@pytest.mark.parametrize(
    ("process_value", "expected"),
    [(None, "384"), ("512", "512")],
    ids=["dotenv", "process-overrides-dotenv"],
)
def test_cli_entry_exports_the_embedding_dimension_before_graphiti_loads(
    tmp_path: Path, process_value: str | None, expected: str
) -> None:
    (tmp_path / ".env").write_text("FAKEYOU_MEMORY__EMBEDDER__DIM=384\n")
    script = (
        "import sys\n"
        "from fakeyou.cli.entry import main\n"
        "sys.argv = ['fakeyou', '--help']\n"
        "try:\n"
        "    main()\n"
        "except SystemExit:\n"
        "    pass\n"
        "from graphiti_core.embedder.client import EMBEDDING_DIM\n"
        "print(EMBEDDING_DIM)\n"
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"EMBEDDING_DIM", "PYTHON_DOTENV_DISABLED"} and not key.startswith("FAKEYOU_")
    }
    if process_value is not None:
        environment["FAKEYOU_MEMORY__EMBEDDER__DIM"] = process_value
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == expected


def test_vad_defaults_mirror_the_vad_params_defaults() -> None:
    from fakeyou.audio.vad import VadParams

    vad = Settings(**env_file_opt(None)).vad
    expected = VadParams()
    assert vad.confidence == expected.confidence
    assert vad.start_secs == expected.start_secs
    assert vad.stop_secs == expected.stop_secs
    assert vad.min_volume == expected.min_volume


def test_vad_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_VAD__START_SECS", "0.5")
    monkeypatch.setenv("FAKEYOU_VAD__MIN_VOLUME", "0.4")
    vad = Settings(**env_file_opt(None)).vad
    assert vad.start_secs == 0.5
    assert vad.min_volume == 0.4
    assert vad.confidence == 0.7
    assert vad.stop_secs == 0.2


def test_join_retry_settings_default_and_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(**env_file_opt(None)).meeting
    assert defaults.join_max_attempts == 3
    assert defaults.join_retry_base_seconds == 2.0
    assert defaults.join_retry_cap_seconds == 30.0
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_RETRY_BASE_SECONDS", "1.5")
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_RETRY_CAP_SECONDS", "10.0")
    retry = Settings(**env_file_opt(None)).meeting
    assert retry.join_max_attempts == 5
    assert retry.join_retry_base_seconds == 1.5
    assert retry.join_retry_cap_seconds == 10.0


def test_join_max_attempts_rejects_zero() -> None:
    with pytest.raises(ValueError, match="Input should be greater than or equal to 1"):
        MeetingSettings.model_validate({"join_max_attempts": 0})


def test_voiceprint_defaults_follow_identity_settings() -> None:
    assert DEFAULTS.identity.voiceprint_enabled is False
    assert DEFAULTS.identity.voiceprint_threshold == 0.60
    assert DEFAULTS.identity.voiceprint_min_margin == 0.15
    assert DEFAULTS.identity.voiceprint_min_seconds == 3.0
    assert DEFAULTS.identity.voiceprint_retention_days == 90
    assert DEFAULTS.identity.voiceprint_model_path == ""
    assert (
        DEFAULTS.identity.voiceprint_model_sha256
        == "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
    )
    assert DEFAULTS.identity.voiceprint_policy_version == "v1"
    assert IdentitySettings() == DEFAULTS.identity


def test_env_example_is_valid_when_its_optional_blocks_are_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = Path(__file__).resolve().parent.parent / ".env.example"
    enabled = []
    for line in example.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"#\s*(FAKEYOU_[A-Z0-9_]+=.*)", line)
        if match and "EXTRA_BODY" not in match.group(1):
            enabled.append(match.group(1))
        else:
            enabled.append(line)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(enabled) + "\n", encoding="utf-8")
    monkeypatch.setenv("FAKEYOU_CONVERSATION__RESPONSE_TIMEOUT_SECONDS", "300")

    Settings(**env_file_opt(env_file))


@pytest.mark.parametrize(
    ("field", "value"),
    [("idle_reconnect_seconds", 90_000.0), ("diarize_max_retries", 11)],
)
def test_stt_reconnect_and_retry_limits_have_ceilings(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        STTSettings.model_validate({field: value})


def test_google_reply_accepts_minimal_reasoning_effort_for_models_that_support_it() -> None:
    from fakeyou.config.settings import GoogleReply

    reply = GoogleReply.model_validate(
        {"model": "gemini-3.1-flash-lite", "reasoning_effort": "minimal"}
    )

    assert reply.reasoning_effort == "minimal"


def test_api_settings_default_to_loopback_without_tokens() -> None:
    api = Settings(**env_file_opt(None)).api
    assert api.host == "127.0.0.1"
    assert api.port == 8000
    assert api.admin_token is None
    assert api.owner_token is None
    assert api.rate_limit_per_minute == 120


def test_api_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_API__HOST", "0.0.0.0")
    monkeypatch.setenv("FAKEYOU_API__PORT", "9001")
    monkeypatch.setenv("FAKEYOU_API__ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("FAKEYOU_API__OWNER_TOKEN", "owner-secret")
    monkeypatch.setenv("FAKEYOU_API__RATE_LIMIT_PER_MINUTE", "30")
    api = Settings(**env_file_opt(None)).api
    assert api.host == "0.0.0.0"
    assert api.port == 9001
    assert api.admin_token is not None
    assert api.admin_token.get_secret_value() == "admin-secret"
    assert api.owner_token is not None
    assert api.owner_token.get_secret_value() == "owner-secret"
    assert api.rate_limit_per_minute == 30


def test_redis_settings_default_and_read_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = Settings(**env_file_opt(None)).transport.redis
    assert redis.url is None
    assert redis.stream_max_len == 10000
    assert redis.lease_seconds == 30.0
    assert redis.connect_timeout_seconds == 5.0
    monkeypatch.setenv("FAKEYOU_TRANSPORT__REDIS__URL", "redis://localhost:6379/0")
    monkeypatch.setenv("FAKEYOU_TRANSPORT__REDIS__STREAM_MAX_LEN", "500")
    redis = Settings(**env_file_opt(None)).transport.redis
    assert redis.url is not None
    assert redis.url.get_secret_value() == "redis://localhost:6379/0"
    assert redis.stream_max_len == 500


def test_extraction_concurrency_defaults_to_frozen_twenty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(**env_file_opt(None)).memory.ingestion.extraction_concurrency == 20
    monkeypatch.setenv("FAKEYOU_MEMORY__INGESTION__EXTRACTION_CONCURRENCY", "4")
    assert Settings(**env_file_opt(None)).memory.ingestion.extraction_concurrency == 4


def test_memory_store_has_no_group_field() -> None:
    assert "group" not in Settings(**env_file_opt(None)).memory.store.model_fields


def test_example_env_file_loads_under_the_schema(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parent.parent / ".env.example"
    active = "\n".join(
        line
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    env_file = tmp_path / ".env"
    env_file.write_text(active, encoding="utf-8")
    settings = Settings(**env_file_opt(env_file))
    assert settings.api.host == "127.0.0.1"
    assert settings.turn_judge.model is None
