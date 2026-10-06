import os
import subprocess
import sys
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
    assert settings.browser.license_key is None


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


def test_conversation_sentence_streaming_defaults_off():
    assert Settings().conversation.sentence_streaming is False


def test_conversation_sentence_streaming_reads_environment(monkeypatch):
    monkeypatch.setenv("FAKEYOU_CONVERSATION__SENTENCE_STREAMING", "true")
    assert Settings().conversation.sentence_streaming is True


def test_importing_fakeyou_disables_implicit_dotenv_loading(tmp_path):
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


@pytest.mark.parametrize("process_value,expected", [(None, "384"), ("512", "512")])
def test_cli_entry_exports_dotenv_before_graphiti_loads(tmp_path, process_value, expected):
    (tmp_path / ".env").write_text("EMBEDDING_DIM=384\n")
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
        environment["EMBEDDING_DIM"] = process_value
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines()[-1] == expected


def test_recall_strategy_defaults_to_prefetch_and_rejects_unknown_values():
    assert Settings(_env_file=None).memory.recall.strategy == "prefetch"
    with pytest.raises(ValueError):
        Settings(_env_file=None, memory={"recall": {"strategy": "hybrid"}})


def test_vad_defaults_mirror_pipecat():
    from pipecat.audio.vad.vad_analyzer import VADParams

    vad = Settings(_env_file=None).vad
    expected = VADParams()
    assert vad.confidence == expected.confidence
    assert vad.start_secs == expected.start_secs
    assert vad.stop_secs == expected.stop_secs
    assert vad.min_volume == expected.min_volume


def test_vad_reads_environment(monkeypatch):
    monkeypatch.setenv("FAKEYOU_VAD__START_SECS", "0.5")
    monkeypatch.setenv("FAKEYOU_VAD__MIN_VOLUME", "0.4")
    vad = Settings(_env_file=None).vad
    assert vad.start_secs == 0.5
    assert vad.min_volume == 0.4
    assert vad.confidence == 0.7
    assert vad.stop_secs == 0.2
