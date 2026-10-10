import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS, IdentitySettings, Settings, require_secret


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
    assert settings.reply.api_key.get_secret_value() == "reply-secret"
    assert settings.stt.api_key.get_secret_value() == "stt-secret"
    assert settings.tts.api_key is None


def test_env_example_values_match_settings_defaults() -> None:
    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert Settings(_env_file=example).model_dump() == Settings(_env_file=None).model_dump()


def test_conversation_sentence_streaming_defaults_off() -> None:
    assert Settings().conversation.sentence_streaming is False


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


def test_recall_strategy_defaults_to_prefetch_and_rejects_unknown_values() -> None:
    assert Settings(_env_file=None).memory.recall.strategy == "prefetch"
    with pytest.raises(ValueError, match="Input should be 'tool' or 'prefetch'"):
        Settings(_env_file=None, memory={"recall": {"strategy": "hybrid"}})


def test_vad_defaults_mirror_pipecat() -> None:
    from pipecat.audio.vad.vad_analyzer import VADParams

    vad = Settings(_env_file=None).vad
    expected = VADParams()
    assert vad.confidence == expected.confidence
    assert vad.start_secs == expected.start_secs
    assert vad.stop_secs == expected.stop_secs
    assert vad.min_volume == expected.min_volume


def test_vad_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_VAD__START_SECS", "0.5")
    monkeypatch.setenv("FAKEYOU_VAD__MIN_VOLUME", "0.4")
    vad = Settings(_env_file=None).vad
    assert vad.start_secs == 0.5
    assert vad.min_volume == 0.4
    assert vad.confidence == 0.7
    assert vad.stop_secs == 0.2


def test_join_retry_settings_default_and_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = Settings(_env_file=None).meeting
    assert defaults.join_max_attempts == 3
    assert defaults.join_retry_base_seconds == 2.0
    assert defaults.join_retry_cap_seconds == 30.0
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_RETRY_BASE_SECONDS", "1.5")
    monkeypatch.setenv("FAKEYOU_MEETING__JOIN_RETRY_CAP_SECONDS", "10.0")
    retry = Settings(_env_file=None).meeting
    assert retry.join_max_attempts == 5
    assert retry.join_retry_base_seconds == 1.5
    assert retry.join_retry_cap_seconds == 10.0


def test_join_max_attempts_rejects_zero() -> None:
    with pytest.raises(ValueError, match="Input should be greater than or equal to 1"):
        Settings(_env_file=None, meeting={"join_max_attempts": 0})


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

    Settings(_env_file=env_file)
