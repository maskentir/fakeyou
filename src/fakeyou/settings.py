from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fakeyou.session import DEFAULT_CHECKPOINT_PATH, PositiveSeconds, SessionId, TwinName

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
TTSProvider = Literal["openai", "minimax"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FAKEYOU_", extra="ignore", hide_input_in_errors=True
    )

    chat_model: str = "openai:gpt-4.1-mini"
    chat_base_url: str | None = None
    chat_api_key: SecretStr | None = None
    chat_reasoning_effort: ReasoningEffort | None = None
    chat_extra_body: dict[str, Any] | None = None
    fallback_chat_model: str | None = None
    fallback_chat_base_url: str | None = None
    fallback_chat_api_key: SecretStr | None = None
    fallback_chat_reasoning_effort: ReasoningEffort | None = None
    fallback_chat_extra_body: dict[str, Any] | None = None
    memory_llm_model: str = "gpt-5.5"
    memory_small_model: str = "gpt-4.1-nano"
    memory_llm_base_url: str | None = None
    memory_llm_api_key: SecretStr | None = None
    memory_embedding_model: str = "text-embedding-3-small"
    memory_embedding_base_url: str | None = None
    memory_embedding_api_key: SecretStr | None = None
    memory_embedding_dim: int = Field(default=1024, gt=0)
    stt_model: str = "muse-voice-transcribe-1.0"
    tts_provider: TTSProvider = "openai"
    tts_api_key: SecretStr | None = None
    tts_voice: str | None = None
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH
    headless: bool = False
    cloakbrowser_license_key: str | None = Field(default=None, repr=False)
    openai_api_key: SecretStr | None = None
    model_api_key: SecretStr | None = None
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr | None = None
    memory_group: str = "local_twin"
    owner_id: SessionId = "local_owner"
    twin_id: SessionId = "local_twin"
    twin_name: TwinName = "Fakeyou Twin"
    lobby_timeout_seconds: PositiveSeconds = 300.0
    session_duration_seconds: PositiveSeconds = 7200.0
    journal_dir: Path = Path(".runtime/journals")
    meeting_record_dir: Path = Path(".runtime/meetings")


def require_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"Set {name} in .env before using this command.")
    return value.get_secret_value()
