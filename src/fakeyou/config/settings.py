import ipaddress
import os
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self, get_args, get_origin, override
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource

from fakeyou.catalog.config import (
    API_KEY_OF_ROLE,
    COMPOSE_ONLY_KEYS,
    ROLE_ATTRIBUTE_PATH,
    ConfigKey,
)
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionIdField, parse_session_id
from fakeyou.foundation.invariants import PLAYBACK_SAMPLE_RATE, PositiveSeconds, TwinName

ReasoningEffort = Literal["minimal", "low", "medium", "high"]
STTMode = Literal["ENDPOINTING", "PUSH_TO_TALK", "DIARIZATION"]
OPENAI_VOICES = frozenset(
    {
        "alloy",
        "ash",
        "ballad",
        "cedar",
        "coral",
        "echo",
        "fable",
        "marin",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    }
)

_SECURE_ENDPOINT_SCHEMES = frozenset({"https", "wss"})


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _require_secure_endpoint(value: Any, info: ValidationInfo) -> Any:
    if not isinstance(value, str) or value == "":
        return value
    try:
        parsed = urlsplit(value)
    except (ValueError, UnicodeError):
        raise ValueError(
            f"{info.field_name} must use https or wss "
            "(http/ws allowed only for loopback or dotless service hosts)"
        ) from None
    host = parsed.hostname or ""
    if parsed.scheme.lower() in _SECURE_ENDPOINT_SCHEMES and host:
        return value
    if host and (_is_loopback_host(host) or "." not in host):
        return value
    raise ValueError(
        f"{info.field_name} must use https or wss "
        "(http/ws allowed only for loopback or dotless service hosts)"
    )


def _optional_secure_url(value: Any, info: ValidationInfo) -> Any:
    if value == "":
        return None
    return _require_secure_endpoint(value, info)


OptionalURL = Annotated[str | None, BeforeValidator(_optional_secure_url)]
SecureURL = Annotated[str, BeforeValidator(_require_secure_endpoint)]


def export_dotenv() -> None:
    for key, value in dotenv_values(Path(".env")).items():
        if value is not None:
            os.environ.setdefault(key, value)


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
    owner_id: SessionIdField = parse_session_id("local_owner", field="owner_id")
    twin_id: SessionIdField = parse_session_id("local_twin", field="twin_id")
    twin_name: TwinName = "Fakeyou Twin"
    voiceprint_enabled: bool = False
    voiceprint_threshold: float = 0.60
    voiceprint_min_margin: float = 0.15
    voiceprint_min_seconds: float = 3.0
    voiceprint_retention_days: int = 90
    voiceprint_model_path: str = ""
    voiceprint_model_sha256: str = (
        "c59158379255ad66e161679cca6af8d52d51e389e3224ab7d7a7baae295c2db5"
    )
    voiceprint_policy_version: str = "v1"
    voiceprint_calibration_path: str = ""
    voiceprint_timeout_seconds: PositiveSeconds = 5.0


class PathSettings(Section):
    journal_dir: Path = Path(".runtime/journals")
    record_dir: Path = Path(".runtime/meetings")
    browser_profile_dir: Path = Path(".runtime/profiles")
    browser_output_dir: Path = Path(".runtime/browser")


class DatabaseSettings(Section):
    url: SecretStr | None = None
    connect_timeout_seconds: PositiveSeconds = 5.0
    statement_timeout_seconds: PositiveSeconds = 30.0
    send_timeout_seconds: PositiveSeconds = 5.0
    keepalive_idle_seconds: PositiveSeconds = 30.0


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
    base_url: SecureURL = Field(min_length=1)
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
    model: str = "gemini-3.1-flash-lite"
    base_url: OptionalURL = "https://generativelanguage.googleapis.com"
    reasoning_effort: ReasoningEffort | None = "minimal"


ReplySettings = Annotated[
    OpenAIReply | CompatibleReply | AnthropicReply | GoogleReply, Field(discriminator="provider")
]


class TTSBase(Section):
    api_key: SecretStr | None = None
    sample_rate: int = PLAYBACK_SAMPLE_RATE
    languages: ClassVar[frozenset[str]] = frozenset({"id", "en"})

    @model_validator(mode="after")
    def playback_rate(self) -> Self:
        if self.sample_rate != PLAYBACK_SAMPLE_RATE:
            raise ValueError("TTS sample_rate must equal playback sample rate")
        return self


class OpenAITTS(TTSBase):
    provider: Literal["openai"] = "openai"
    model: str = "gpt-4o-mini-tts"
    voice: str = "alloy"
    base_url: SecureURL = "https://api.openai.com/v1"

    @model_validator(mode="after")
    def valid_voice(self) -> Self:
        if self.voice not in OPENAI_VOICES:
            raise ValueError("unsupported OpenAI voice")
        return self


class MiniMaxTTS(TTSBase):
    provider: Literal["minimax"] = "minimax"
    model: str = "speech-2.8-turbo"
    voice: str = "Indonesian_CalmWoman"
    base_url: SecureURL = "https://api.minimax.io/v1/t2a_v2"


class CartesiaTTS(TTSBase):
    provider: Literal["cartesia"] = "cartesia"
    model: str = "sonic-3"
    voice: str = "f3b7c445-d015-40f7-a665-9595e0bc0c90"
    base_url: SecureURL = "https://api.cartesia.ai"


class ElevenLabsTTS(TTSBase):
    provider: Literal["elevenlabs"] = "elevenlabs"
    model: str = "eleven_multilingual_v2"
    voice: str = Field(min_length=1)
    base_url: SecureURL = "https://api.elevenlabs.io"


TTSSettings = Annotated[
    OpenAITTS | MiniMaxTTS | CartesiaTTS | ElevenLabsTTS, Field(discriminator="provider")
]


class STTSettings(Section):
    provider: Literal["muse"] = "muse"
    model: str = "muse-voice-transcribe-1.0"
    api_key: SecretStr | None = None
    url: SecureURL = "wss://api.meta.ai/v1/asr/realtime"
    mode: STTMode = "DIARIZATION"
    idle_reconnect_seconds: float = Field(default=0.0, ge=0.0, le=86_400.0)
    ready_timeout_seconds: PositiveSeconds = Field(default=10.0, le=300.0)
    diarize_timeout_seconds: PositiveSeconds = 60.0
    diarize_max_retries: int = Field(default=3, ge=0, le=10)


class VADSettings(Section):
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    start_secs: float = Field(default=0.2, ge=0.0, le=60.0)
    stop_secs: float = Field(default=0.2, ge=0.0, le=60.0)
    min_volume: float = Field(default=0.6, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def non_empty_window(self) -> Self:
        if self.start_secs + self.stop_secs <= 0:
            raise ValueError("VAD requires a non-empty start/stop window")
        return self


class StoreSettings(Section):
    uri: str = "bolt://localhost:7687"
    user: str = "neo4j"
    password: SecretStr | None = None
    database: str = "neo4j"


class MemoryLLMBase(EndpointSettings):
    model: str = "gpt-5.5"
    small_model: str = "gpt-4.1-nano"
    api_key: SecretStr | None = None
    timeout_seconds: PositiveSeconds = 60.0
    max_retries: int = Field(default=2, ge=0)


class OpenAIMemoryLLM(MemoryLLMBase):
    client: Literal["openai"] = "openai"
    base_url: OptionalURL = "https://api.openai.com/v1"


class GenericMemoryLLM(MemoryLLMBase):
    client: Literal["openai_generic"] = "openai_generic"
    base_url: SecureURL = Field(min_length=1)


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
    timeout_seconds: PositiveSeconds = 60.0
    max_retries: int = Field(default=2, ge=0)


class RecallSettings(Section):
    result_limit: int = Field(default=5, gt=0)
    timeout_seconds: PositiveSeconds = 15.0

    @property
    def search_limit(self) -> int:
        return self.result_limit * 2


class IngestionSettings(Section):
    window_utterances: int = Field(default=20, gt=0)
    window_seconds: PositiveSeconds = 60.0
    episode_timeout_seconds: PositiveSeconds = 120.0
    extraction_concurrency: int = Field(default=20, gt=0)


class MemorySettings(Section):
    store: StoreSettings = Field(default_factory=StoreSettings)
    llm: MemoryLLMSettings = Field(default_factory=OpenAIMemoryLLM)
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    recall: RecallSettings = Field(default_factory=RecallSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    close_callback_seconds: PositiveSeconds = 5.0
    close_hard_seconds: PositiveSeconds = 30.0


class ConversationSettings(Section):
    response_timeout_seconds: PositiveSeconds = 60.0
    barge_in_min_words: int = Field(default=3, gt=0)
    barge_in_judge_enabled: bool = True
    max_spoken_sentences: int = Field(default=2, gt=0)
    sentence_streaming: bool = True
    thinking_cue_enabled: bool = True
    thinking_cue_after_seconds: float = Field(default=2.0, ge=0.0, le=10.0)
    participant_evidence_max_bytes: int = Field(default=8388608, gt=0)
    voice_labels_enabled: bool = False
    meet_chat_per_minute: int = Field(default=3, gt=0)
    per_stream_enabled: bool = False
    history_max_tokens: int = Field(default=32000, gt=0)


class TurnJudgeSettings(Section):
    model: ReplySettings | None = None
    timeout_seconds: PositiveSeconds = 4.0
    window_lines: int = Field(default=8, gt=0, le=30)
    reply_threshold: float = Field(default=0.60, gt=0, lt=1)
    clarify_threshold: float = Field(default=0.50, gt=0, lt=1)
    barge_in_threshold: float = Field(default=0.60, gt=0, lt=1)
    unnamed_reply_threshold: float = Field(default=0.85, gt=0, lt=1)
    early_commit_enabled: bool = True
    commit_hold_seconds: float = Field(default=0.3, ge=0.0, le=3.0)


class MeetingSettings(Section):
    lobby_timeout_seconds: PositiveSeconds = 300.0
    session_duration_seconds: PositiveSeconds = 7200.0
    join_poll_seconds: PositiveSeconds = 2.0
    monitor_poll_seconds: PositiveSeconds = 5.0
    leave_timeout_seconds: PositiveSeconds = 30.0
    join_max_attempts: int = Field(default=3, ge=1)
    join_retry_base_seconds: PositiveSeconds = 2.0
    join_retry_cap_seconds: PositiveSeconds = 30.0
    alone_leave_seconds: PositiveSeconds = 600.0


class BrowserSettings(Section):
    headless: bool = False
    warmup: bool = True
    license_key: SecretStr | None = None


class AudioSettings(Section):
    persist_enabled: bool = False
    retention_days: int = Field(default=30, gt=0)
    drain_max_consecutive_failures: int = Field(default=40, gt=0)
    drain_failure_journal_every: int = Field(default=10, gt=0)
    pactl_timeout_seconds: PositiveSeconds = 5.0


class AudioHealthSettings(Section):
    deaf_window_seconds: PositiveSeconds = 120.0
    deaf_grace_seconds: PositiveSeconds = 120.0


class PreflightSettings(Section):
    budget_seconds: PositiveSeconds = 5.0


class WorkerSettings(Section):
    audio: bool = False


class PerceptionInterpreterSettings(Section):
    provider: Literal["openai", "openai_compatible", "anthropic", "google"] = "google"
    model: str = "gemini-3.8-flash"
    base_url: OptionalURL = None
    api_key: SecretStr | None = None
    timeout_seconds: PositiveSeconds = 30.0
    max_retries: int = Field(default=2, ge=0)
    max_concurrent: int = Field(default=2, gt=0)


class AttentionSettings(Section):
    enabled: bool = True
    max_interjections_per_10min: int = Field(default=2, ge=0)
    floor_gap_seconds: PositiveSeconds = 1.2
    ttl_seconds: PositiveSeconds = 30.0
    transcript_moments: bool = True
    unanswered_seconds: PositiveSeconds = 15.0
    owner_names: tuple[str, ...] = ()
    moment_confidence: float = Field(default=0.8, gt=0, lt=1)
    grounding_confidence: float = Field(default=0.7, gt=0, lt=1)
    similarity_threshold: float = Field(default=0.15, gt=0, lt=1)


class PerceptionSettings(Section):
    enabled: bool = True
    screen: bool = True
    chat: bool = True
    signals: bool = True
    cameras: bool = True
    interpreter: PerceptionInterpreterSettings = Field(
        default_factory=PerceptionInterpreterSettings
    )
    budget_usd: float = Field(default=1.0, ge=0)
    max_calls: int = Field(default=240, ge=0)
    view_max_chars: int = Field(default=3000, gt=0)
    retention_raw_days: int = Field(default=7, gt=0)
    retention_text_days: int = Field(default=30, gt=0)
    attention: AttentionSettings = Field(default_factory=AttentionSettings)

    @model_validator(mode="after")
    def compatible_provider_needs_base_url(self) -> Self:
        interpreter = self.interpreter
        if (
            self.enabled
            and interpreter.provider == "openai_compatible"
            and interpreter.base_url is None
        ):
            raise ValueError(
                "perception provider openai_compatible requires "
                "FAKEYOU_PERCEPTION__INTERPRETER__BASE_URL"
            )
        return self


class ApiSettings(Section):
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    admin_token: SecretStr | None = None
    owner_token: SecretStr | None = None
    rate_limit_per_minute: int = Field(default=120, gt=0)


class RedisSettings(Section):
    url: SecretStr | None = None
    stream_max_len: int = Field(default=10000, gt=0)
    lease_seconds: PositiveSeconds = 30.0
    connect_timeout_seconds: PositiveSeconds = 5.0


class TransportSettings(Section):
    redis: RedisSettings = Field(default_factory=RedisSettings)


class Configuration(Section):
    identity: IdentitySettings = Field(default_factory=IdentitySettings)
    paths: PathSettings = Field(default_factory=PathSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    reply: ReplySettings = Field(default_factory=OpenAIReply)
    reply_fallback: ReplySettings | None = None
    reply_strong: ReplySettings | None = None
    debrief: ReplySettings | None = None
    tts: TTSSettings = Field(default_factory=OpenAITTS)
    tts_fallback: TTSSettings | None = None
    stt: STTSettings = Field(default_factory=STTSettings)
    vad: VADSettings = Field(default_factory=VADSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    turn_judge: TurnJudgeSettings = Field(default_factory=TurnJudgeSettings)
    meeting: MeetingSettings = Field(default_factory=MeetingSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    audio_health: AudioHealthSettings = Field(default_factory=AudioHealthSettings)
    preflight: PreflightSettings = Field(default_factory=PreflightSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    perception: PerceptionSettings = Field(default_factory=PerceptionSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    transport: TransportSettings = Field(default_factory=TransportSettings)

    @model_validator(mode="after")
    def response_budget(self) -> Self:
        primary = self.reply_strong or self.reply
        roles = [primary] + ([self.reply_fallback] if self.reply_fallback else [])
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
            path = ROLE_ATTRIBUTE_PATH.get(role)
            if path is None:
                raise FakeYouError(
                    ErrorCode.CONFIG_INVALID, reason=f"unknown provider role: {role}"
                )
            value: Any = self
            for part in path.split("."):
                value = getattr(value, part)
            if value is None:
                continue
            if role == "perception" and not self.perception.enabled:
                continue
            require_secret(value.api_key, API_KEY_OF_ROLE[role])


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


_COMPOSE_ONLY_NAMES = frozenset(key.value for key in COMPOSE_ONLY_KEYS)


class Settings(Configuration, BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FAKEYOU_",
        env_nested_delimiter="__",
        extra="forbid",
        dotenv_filtering="match_prefix",
        hide_input_in_errors=True,
    )

    @model_validator(mode="after")
    def speech_language_is_supported_by_tts(self) -> Self:
        for tts in (self.tts, self.tts_fallback):
            if tts is not None and self.speech.language not in tts.languages:
                raise ValueError(
                    f"speech language {self.speech.language!r} is not supported by "
                    f"the {tts.provider!r} TTS provider"
                )
        return self

    @classmethod
    @override
    def settings_customise_sources(
        cls,
        _settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        for source in (env_settings, dotenv_settings):
            if isinstance(source, EnvSettingsSource):
                for key in list(source.env_vars):
                    upper = key.upper()
                    if upper in _COMPOSE_ONLY_NAMES:
                        source.env_vars = {
                            name: value for name, value in source.env_vars.items() if name != key
                        }
                    elif upper.startswith("FAKEYOU_") and not _known_env_path(
                        cls, key.lower().removeprefix("fakeyou_").split("__")
                    ):
                        raise FakeYouError(ErrorCode.CONFIG_UNKNOWN_KEY, name=upper)
        return init_settings, env_settings, dotenv_settings, file_secret_settings


def require_secret(value: SecretStr | None, name: ConfigKey) -> str:
    if value is None or not value.get_secret_value().strip():
        raise FakeYouError(ErrorCode.CONFIG_SECRET_MISSING, name=name)
    return value.get_secret_value()
