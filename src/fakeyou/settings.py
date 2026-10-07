from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

from fakeyou.invariants import PLAYBACK_SAMPLE_RATE, PositiveSeconds, SessionId, TwinName

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
STTMode = Literal["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"]
OptionalURL = Annotated[str | None, BeforeValidator(lambda value: None if value == "" else value)]


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EndpointSettings(Section):
    base_url: OptionalURL = None

    @property
    def resolved_base_url(self) -> str:
        value = self.base_url
        if value is None:
            value = type(self).model_fields["base_url"].default
        if not isinstance(value, str) or not value:
            raise ValueError("base_url must be configured for this provider")
        return value


class IdentitySettings(Section):
    owner_id: SessionId = "local_owner"
    twin_id: SessionId = "local_twin"
    twin_name: TwinName = "Fakeyou Twin"


class PathSettings(Section):
    journal_dir: Path = Path(".runtime/journals")
    record_dir: Path = Path(".runtime/meetings")
    checkpoint_path: Path = Path(".runtime/checkpoints.sqlite")
    browser_profile_dir: Path = Path(".runtime/profiles")


class CheckpointSettings(Section):
    url: SecretStr | None = None


class SpeechSettings(Section):
    language: Literal["id", "en"] = "id"


class ReplyBase(EndpointSettings):
    model: str = "gpt-4.1-mini"
    api_key: SecretStr | None = None
    timeout_seconds: PositiveSeconds = 15.0
    max_retries: int = Field(default=2, ge=0)

    @model_validator(mode="after")
    def unprefixed_model(self) -> Self:
        if ":" in self.model:
            raise ValueError(
                "model must not contain a provider prefix; configure provider explicitly"
            )
        return self


class OpenAIReply(ReplyBase):
    langchain_provider: ClassVar[str] = "openai"
    provider: Literal["openai"] = "openai"
    base_url: OptionalURL = "https://api.openai.com/v1"
    reasoning_effort: ReasoningEffort | None = None


class CompatibleReply(ReplyBase):
    langchain_provider: ClassVar[str] = "openai"
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(min_length=1)
    reasoning_effort: ReasoningEffort | None = None
    extra_body: dict[str, Any] | None = None


class AnthropicReply(ReplyBase):
    langchain_provider: ClassVar[str] = "anthropic"
    provider: Literal["anthropic"] = "anthropic"
    model: str = "claude-haiku-4-5-20251001"
    base_url: OptionalURL = "https://api.anthropic.com"


class GoogleReply(ReplyBase):
    langchain_provider: ClassVar[str] = "google_genai"
    provider: Literal["google"] = "google"
    model: str = "gemini-3.8-flash"
    base_url: OptionalURL = "https://generativelanguage.googleapis.com"
    reasoning_effort: ReasoningEffort | None = None


ReplySettings = Annotated[
    OpenAIReply | CompatibleReply | AnthropicReply | GoogleReply, Field(discriminator="provider")
]


class TTSBase(Section):
    api_key: SecretStr | None = None
    sample_rate: int = PLAYBACK_SAMPLE_RATE

    @model_validator(mode="after")
    def playback_rate(self) -> Self:
        if self.sample_rate != PLAYBACK_SAMPLE_RATE:
            raise ValueError("TTS sample_rate must equal playback sample rate")
        return self


class OpenAITTS(TTSBase):
    provider: Literal["openai"] = "openai"
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    base_url: str = "https://api.openai.com/v1"

    @model_validator(mode="after")
    def valid_voice(self) -> Self:
        from pipecat.services.openai.tts import VALID_VOICES

        if self.voice not in VALID_VOICES:
            raise ValueError("unsupported OpenAI voice")
        return self


class MiniMaxTTS(TTSBase):
    provider: Literal["minimax"] = "minimax"
    model: str = "speech-2.8-turbo"
    voice: str = "Indonesian_CalmWoman"
    base_url: str = "https://api.minimax.io/v1/t2a_v2"


TTSSettings = Annotated[OpenAITTS | MiniMaxTTS, Field(discriminator="provider")]


class STTSettings(Section):
    provider: Literal["muse"] = "muse"
    model: str = "muse-voice-transcribe-1.0"
    api_key: SecretStr | None = None
    url: str = "wss://api.meta.ai/v1/asr/realtime"
    mode: STTMode = "ENDPOINTING"
    preroll_ms: float = 500.0
    idle_reconnect_seconds: float = 0.0
    ready_timeout_seconds: float = 10.0


class VADSettings(Section):
    confidence: float = 0.7
    start_secs: float = 0.2
    stop_secs: float = 0.2
    min_volume: float = 0.6


class StoreSettings(Section):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: SecretStr | None = None
    database: str = "neo4j"
    group: str = "local_twin"


class MemoryLLMBase(EndpointSettings):
    model: str = "gpt-5.5"
    small_model: str = "gpt-4.1-nano"
    api_key: SecretStr | None = None


class OpenAIMemoryLLM(MemoryLLMBase):
    client: Literal["openai"] = "openai"
    base_url: OptionalURL = "https://api.openai.com/v1"


class GenericMemoryLLM(MemoryLLMBase):
    client: Literal["openai_generic"] = "openai_generic"
    base_url: str = Field(min_length=1)


class AnthropicMemoryLLM(MemoryLLMBase):
    model: str = "claude-haiku-4-5-20251001"
    small_model: str = "claude-haiku-4-5-20251001"
    client: Literal["anthropic"] = "anthropic"
    base_url: OptionalURL = "https://api.anthropic.com"


MemoryLLMSettings = Annotated[
    OpenAIMemoryLLM | GenericMemoryLLM | AnthropicMemoryLLM, Field(discriminator="client")
]


class EmbedderSettings(EndpointSettings):
    model: str = "text-embedding-3-small"
    api_key: SecretStr | None = None
    base_url: OptionalURL = "https://api.openai.com/v1"
    dim: int = Field(default=1024, gt=0)


RecallStrategy = Literal["tool", "prefetch"]


class RecallSettings(Section):
    result_limit: int = Field(default=5, gt=0)
    timeout_seconds: PositiveSeconds = 15.0
    strategy: RecallStrategy = "prefetch"

    @property
    def search_limit(self) -> int:
        return self.result_limit * 2


class IngestionSettings(Section):
    window_utterances: int = Field(default=20, gt=0)
    window_seconds: PositiveSeconds = 60.0
    episode_timeout_seconds: PositiveSeconds = 120.0


class MemorySettings(Section):
    store: StoreSettings = Field(default_factory=StoreSettings)
    llm: MemoryLLMSettings = Field(default_factory=OpenAIMemoryLLM)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    recall: RecallSettings = Field(default_factory=RecallSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)


class ConversationSettings(Section):
    response_timeout_seconds: PositiveSeconds = 60.0
    barge_in_min_words: int = Field(default=3, gt=0)
    max_spoken_sentences: int = Field(default=2, gt=0)
    activation_seconds: PositiveSeconds = 30.0
    sentence_streaming: bool = False
    participant_policy_enabled: bool = False
    attribution_probe_enabled: bool = False
    voice_labels_enabled: bool = False
    per_stream_enabled: bool = False
    pre_generate_enabled: bool = False


class MeetingSettings(Section):
    lobby_timeout_seconds: PositiveSeconds = 300.0
    session_duration_seconds: PositiveSeconds = 7200.0
    join_poll_seconds: PositiveSeconds = 2.0
    monitor_poll_seconds: PositiveSeconds = 5.0
    leave_timeout_seconds: PositiveSeconds = 30.0
    join_max_attempts: int = Field(default=3, ge=1)
    join_retry_base_seconds: PositiveSeconds = 2.0
    join_retry_cap_seconds: PositiveSeconds = 30.0


class BrowserSettings(Section):
    headless: bool = False
    warmup: bool = True
    license_key: SecretStr | None = None


class AudioHealthSettings(Section):
    deaf_window_seconds: PositiveSeconds = 120.0
    deaf_grace_seconds: PositiveSeconds = 120.0
    alone_leave_seconds: PositiveSeconds = 600.0


class PreflightSettings(Section):
    budget_seconds: PositiveSeconds = 5.0


class WorkerSettings(Section):
    audio: bool = False


class Configuration(Section):
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    reply: ReplySettings = Field(default_factory=OpenAIReply)
    reply_fallback: ReplySettings | None = None
    debrief: ReplySettings | None = None
    tts: TTSSettings = Field(default_factory=OpenAITTS)
    stt: STTSettings = Field(default_factory=STTSettings)
    vad: VADSettings = Field(default_factory=VADSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    meeting: MeetingSettings = Field(default_factory=MeetingSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    audio_health: AudioHealthSettings = Field(default_factory=AudioHealthSettings)
    preflight: PreflightSettings = Field(default_factory=PreflightSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)

    @model_validator(mode="after")
    def response_budget(self) -> Self:
        roles = [self.reply] + ([self.reply_fallback] if self.reply_fallback else [])
        budget = sum(role.timeout_seconds * (role.max_retries + 1) for role in roles)
        budget += self.memory.recall.timeout_seconds
        if budget > self.conversation.response_timeout_seconds:
            raise ValueError(
                "reply retries, fallback and recall exceed conversation response budget"
            )
        return self

    @property
    def debrief_role(self) -> ReplySettings:
        return self.debrief or self.reply

    def require_roles(self, *roles: str) -> None:
        for role in roles:
            value: Any = self
            for part in role.split("."):
                value = getattr(value, part)
            if value is not None:
                require_secret(
                    value.api_key, "FAKEYOU_" + role.replace(".", "__").upper() + "__API_KEY"
                )


DEFAULTS = Configuration()


def _known_env_path(annotation: Any, parts: list[str]) -> bool:
    if not parts:
        return True
    if get_origin(annotation) is Annotated:
        return _known_env_path(get_args(annotation)[0], parts)
    if get_origin(annotation) is dict:
        return True
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        field = annotation.model_fields.get(parts[0])
        return field is not None and _known_env_path(field.annotation, parts[1:])
    return any(_known_env_path(option, parts) for option in get_args(annotation))


class Settings(Configuration, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FAKEYOU_",
        env_nested_delimiter="__",
        extra="forbid",
        dotenv_filtering="match_prefix",
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        for source in (env_settings, dotenv_settings):
            if isinstance(source, EnvSettingsSource):
                for key in list(source.env_vars):
                    upper = key.upper()
                    if upper.startswith("FAKEYOU_COMPOSE_"):
                        source.env_vars = {
                            name: value for name, value in source.env_vars.items() if name != key
                        }
                    elif upper.startswith("FAKEYOU_") and not _known_env_path(
                        cls, key.lower().removeprefix("fakeyou_").split("__")
                    ):
                        raise ValueError(f"Unknown configuration key: {upper}")
        return init_settings, env_settings, dotenv_settings, file_secret_settings


def require_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"Set {name} in .env before using this command.")
    return value.get_secret_value()
