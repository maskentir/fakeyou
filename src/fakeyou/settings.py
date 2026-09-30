from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from fakeyou.session import PositiveSeconds, SessionId, TwinName


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="FAKEYOU_", extra="ignore", hide_input_in_errors=True
    )

    chat_model: str = "openai:gpt-4.1-mini"
    checkpoint_path: Path = Path(".runtime/checkpoints.sqlite")
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


def require_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"Set {name} in .env before using this command.")
    return value.get_secret_value()
