import json
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, TypeAdapter

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import (
    AnthropicMemoryLLM,
    AnthropicReply,
    CompatibleReply,
    EmbedderSettings,
    GoogleReply,
    MemorySettings,
    OpenAIMemoryLLM,
    OpenAIReply,
    ReplySettings,
    Settings,
    StoreSettings,
    STTSettings,
    TTSSettings,
    VADSettings,
)
from tests._error_support import message_pattern
from tests._fakes_common import env_file_opt


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("name", ["FAKEYOU_RELPY", "fakeyou_relpy", "FAKEYOU_REPLY__TYPO"])
def test_unknown_process_keys_fail_even_when_empty(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "")
    with pytest.raises(FakeYouError, match="(?i)(relpy|typo)"):
        Settings(**env_file_opt(None))


def test_unknown_key_is_rejected_without_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_CHAT_API_KEY", "never-print-me")
    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.CONFIG_UNKNOWN_KEY)) as error:
        Settings(**env_file_opt(None))
    assert error.value.code is ErrorCode.CONFIG_UNKNOWN_KEY
    assert str(error.value) == "Unknown configuration key: FAKEYOU_CHAT_API_KEY"
    assert "never-print-me" not in str(error.value)


def test_reply_budget_includes_retries_fallback_and_recall() -> None:
    with pytest.raises(ValueError, match="budget"):
        Settings(
            **env_file_opt(None),
            reply=OpenAIReply(provider="openai", timeout_seconds=30, max_retries=2),
        )


@pytest.mark.parametrize(
    "name", ["FAKEYOU_RELPY", "FAKEYOU_REPLY__TYPO", "fakeyou_memory__embedder__dmi"]
)
def test_unknown_dotenv_keys_fail_even_when_empty(tmp_path: Path, name: str) -> None:
    path = tmp_path / ".env"
    path.write_text(f"{name}=\n")
    with pytest.raises(FakeYouError, match="(?i)(relpy|typo|dmi)"):
        Settings(**env_file_opt(path))


def test_base_url_requires_tls_unless_loopback_or_service_name() -> None:
    assert CompatibleReply(base_url="https://example.com/v1").base_url == "https://example.com/v1"
    assert (
        CompatibleReply(base_url="http://127.0.0.1:8000/v1").base_url == "http://127.0.0.1:8000/v1"
    )
    assert (
        CompatibleReply(base_url="http://localhost:8000/v1").base_url == "http://localhost:8000/v1"
    )
    assert CompatibleReply(base_url="http://[::1]:8000/v1").base_url == "http://[::1]:8000/v1"
    assert (
        CompatibleReply(base_url="http://embeddings:8000/v1").base_url
        == "http://embeddings:8000/v1"
    )
    with pytest.raises(ValueError, match="https"):
        CompatibleReply(base_url="http://example.com/v1")
    with pytest.raises(ValueError, match="https"):
        CompatibleReply(base_url="ws://example.com/socket")


def test_optional_base_url_keeps_empty_mapping_and_requires_tls() -> None:
    assert OpenAIReply(base_url="", max_retries=0).base_url is None
    with pytest.raises(ValueError, match="https"):
        OpenAIReply(base_url="http://example.com/v1", max_retries=0)


def test_stt_url_requires_tls_unless_loopback() -> None:
    assert STTSettings(url="wss://example.com/x").url == "wss://example.com/x"
    assert STTSettings(url="ws://127.0.0.1:9/x").url == "ws://127.0.0.1:9/x"
    with pytest.raises(ValueError, match="https"):
        STTSettings(url="ws://example.com/x")


def test_stt_numeric_settings_are_bounded() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        STTSettings(idle_reconnect_seconds=-1.0)
    with pytest.raises(ValueError, match="greater than 0"):
        STTSettings(ready_timeout_seconds=0.0)
    with pytest.raises(ValueError, match="less than or equal to 300"):
        STTSettings(ready_timeout_seconds=301.0)


def test_vad_settings_are_bounded_with_nonempty_window() -> None:
    assert VADSettings().confidence == 0.7
    with pytest.raises(ValueError, match="less than or equal to 1"):
        VADSettings(confidence=1.5)
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        VADSettings(confidence=-0.1)
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        VADSettings(start_secs=-1.0)
    with pytest.raises(ValueError, match="less than or equal to 60"):
        VADSettings(stop_secs=61.0)
    with pytest.raises(ValueError, match="less than or equal to 1"):
        VADSettings(min_volume=2.0)
    with pytest.raises(ValueError, match="non-empty"):
        VADSettings(start_secs=0.0, stop_secs=0.0)


def test_meet_chat_rate_defaults_small_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(**env_file_opt(None)).conversation.meet_chat_per_minute == 3
    monkeypatch.setenv("FAKEYOU_CONVERSATION__MEET_CHAT_PER_MINUTE", "5")
    assert Settings(**env_file_opt(None)).conversation.meet_chat_per_minute == 5


def test_retired_browser_sandbox_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_BROWSER__SANDBOX", "true")
    with pytest.raises(FakeYouError, match="Unknown configuration key"):
        Settings(**env_file_opt(None))


def test_voiceprint_timeout_defaults_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(**env_file_opt(None)).identity.voiceprint_timeout_seconds == 5.0
    monkeypatch.setenv("FAKEYOU_IDENTITY__VOICEPRINT_TIMEOUT_SECONDS", "7.5")
    assert Settings(**env_file_opt(None)).identity.voiceprint_timeout_seconds == 7.5


def test_drain_resilience_limits_default_and_read_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = Settings(**env_file_opt(None)).audio
    assert audio.drain_max_consecutive_failures == 40
    assert audio.drain_failure_journal_every == 10
    monkeypatch.setenv("FAKEYOU_AUDIO__DRAIN_MAX_CONSECUTIVE_FAILURES", "5")
    monkeypatch.setenv("FAKEYOU_AUDIO__DRAIN_FAILURE_JOURNAL_EVERY", "2")
    audio = Settings(**env_file_opt(None)).audio
    assert audio.drain_max_consecutive_failures == 5
    assert audio.drain_failure_journal_every == 2


def test_participant_evidence_cap_defaults_and_reads_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(**env_file_opt(None)).conversation.participant_evidence_max_bytes == 8388608
    monkeypatch.setenv("FAKEYOU_CONVERSATION__PARTICIPANT_EVIDENCE_MAX_BYTES", "1024")
    assert Settings(**env_file_opt(None)).conversation.participant_evidence_max_bytes == 1024


def test_database_timeouts_default_and_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    database = Settings(**env_file_opt(None)).database
    assert database.connect_timeout_seconds == 5.0
    assert database.statement_timeout_seconds == 30.0
    assert database.send_timeout_seconds == 5.0
    assert database.keepalive_idle_seconds == 30.0
    monkeypatch.setenv("FAKEYOU_DATABASE__CONNECT_TIMEOUT_SECONDS", "2.0")
    monkeypatch.setenv("FAKEYOU_DATABASE__STATEMENT_TIMEOUT_SECONDS", "9.0")
    monkeypatch.setenv("FAKEYOU_DATABASE__SEND_TIMEOUT_SECONDS", "3.0")
    monkeypatch.setenv("FAKEYOU_DATABASE__KEEPALIVE_IDLE_SECONDS", "11.0")
    database = Settings(**env_file_opt(None)).database
    assert database.connect_timeout_seconds == 2.0
    assert database.statement_timeout_seconds == 9.0
    assert database.send_timeout_seconds == 3.0
    assert database.keepalive_idle_seconds == 11.0


def test_compose_and_unrelated_library_keys_are_allowed_in_both_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("fakeyou_compose_worker_uri", "ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated")
    path = tmp_path / ".env"
    path.write_text("FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE=example\nLIBRARY_OPTION=value\n")
    settings = Settings(**env_file_opt(path))
    assert settings.reply.api_key is None


@pytest.mark.parametrize(
    "role", ["reply", "reply_fallback", "tts", "stt", "memory.llm", "memory.embedder"]
)
def test_activated_role_never_borrows_a_key(role: str) -> None:
    from fakeyou.config.settings import OpenAIReply

    settings = Settings(
        **env_file_opt(None),
        reply_fallback=OpenAIReply(max_retries=0),
        reply=OpenAIReply(max_retries=0),
    )
    settings.stt.api_key = SecretStr("other-role") if role != "stt" else None
    with pytest.raises(FakeYouError, match="API_KEY") as caught:
        settings.require_roles(role)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_inspection_does_not_require_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from fakeyou.cli.diagnostics import check_setup

    settings = Settings(**env_file_opt(None))
    check_setup(settings)
    assert json.loads(capsys.readouterr().out) == settings.model_dump(mode="json")


def test_check_masks_credentials_in_urls_and_extra_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.cli.diagnostics import check_setup

    settings = Settings(**env_file_opt(None))
    settings.reply = CompatibleReply(
        base_url="https://operator:sekret-pass@custom.example/v1?api_key=sekret-query",
        extra_body={"session_token": "sekret-token", "top_k": 5},
    )
    check_setup(settings)
    report = capsys.readouterr().out

    assert "sekret-pass" not in report
    assert "sekret-query" not in report
    assert "sekret-token" not in report
    assert "operator" not in report
    assert "custom.example" in report
    assert '"top_k": 5' in report


def test_check_masks_credentials_in_any_url_setting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.cli.diagnostics import check_setup

    settings = Settings(**env_file_opt(None))
    settings.memory.store.uri = "bolt://neo4j:graph-secret@graph.example:7687"
    check_setup(settings)
    report = capsys.readouterr().out

    assert "graph-secret" not in report
    assert "neo4j:" not in report
    assert "graph.example" in report


_REPLY_MATCH = {
    '{"model": "custom", "provider": "openai_compatible"}': "Field required",
    '{"model": "openai:gpt-4.1-mini", "provider": "openai"}': (
        "must not contain a provider prefix"
    ),
    '{"extra_body": {}, "provider": "anthropic"}': "Extra inputs are not permitted",
    '{"provider": "anthropic", "reasoning_effort": "minimal"}': ("Extra inputs are not permitted"),
    '{"extra_body": {}, "provider": "google"}': "Extra inputs are not permitted",
    '{"provider": "google", "reasoning_effort": "unlimited"}': (
        "Input should be 'low', 'medium' or 'high'"
    ),
    '{"model": "google_genai:gemini-3.8-flash", "provider": "google"}': (
        "must not contain a provider prefix"
    ),
    '{"provider": "ollama"}': "does not match any of the expected tags",
}


@pytest.mark.parametrize(
    "reply",
    [
        {"provider": "openai_compatible", "model": "custom"},
        {"provider": "openai", "model": "openai:gpt-4.1-mini"},
        {"provider": "anthropic", "extra_body": {}},
        {"provider": "anthropic", "reasoning_effort": "minimal"},
        {"provider": "google", "extra_body": {}},
        {"provider": "google", "reasoning_effort": "unlimited"},
        {"provider": "google", "model": "google_genai:gemini-3.8-flash"},
        {"provider": "ollama"},
    ],
)
def test_invalid_provider_configuration_is_rejected(reply: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=_REPLY_MATCH[json.dumps(reply, sort_keys=True)]):
        TypeAdapter(ReplySettings).validate_python(reply)


def test_google_reply_accepts_reasoning_effort_levels_gemini_supports() -> None:
    from fakeyou.config.settings import GoogleReply

    assert GoogleReply().reasoning_effort == "low"
    assert GoogleReply(reasoning_effort="high").reasoning_effort == "high"


@pytest.mark.parametrize(
    "tts",
    [{"provider": "openai", "voice": "unknown"}, {"provider": "minimax", "sample_rate": 16000}],
)
def test_invalid_voice_or_playback_rate_is_rejected(tts: dict[str, Any]) -> None:
    expected = (
        "unsupported OpenAI voice"
        if tts["provider"] == "openai"
        else "TTS sample_rate must equal playback sample rate"
    )
    with pytest.raises(ValueError, match=expected):
        TypeAdapter(TTSSettings).validate_python(tts)


def test_fallback_and_recall_are_included_in_budget() -> None:
    with pytest.raises(ValueError, match="budget"):
        Settings(
            **env_file_opt(None),
            reply_fallback=OpenAIReply(provider="openai", max_retries=0),
        )
    settings = Settings(
        **env_file_opt(None),
        reply=OpenAIReply(provider="openai", max_retries=0),
        reply_fallback=OpenAIReply(provider="openai", max_retries=1),
    )
    assert settings.conversation.response_timeout_seconds == 60


def test_empty_embedding_url_is_normalized_at_schema_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKEYOU_MEMORY__EMBEDDER__BASE_URL", "")
    assert Settings(**env_file_opt(None)).memory.embedder.base_url is None


async def test_memory_rejects_dimension_different_from_imported_graphiti(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from graphiti_core.embedder.client import EMBEDDING_DIM

    import fakeyou.memory.memory as module
    from fakeyou.memory.memory import open_graphiti_memory

    config = MemorySettings.model_validate(
        {
            "store": {"uri": "bolt://localhost:7687", "user": "test", "password": "test"},
            "llm": {"client": "openai", "model": "llm", "small_model": "small", "api_key": "llm"},
            "embedder": {"model": "embed", "api_key": "embedding", "dim": EMBEDDING_DIM + 1},
        }
    )
    monkeypatch.setattr(
        module,
        "create_graphiti_memory",
        lambda _, **_kwargs: pytest.fail("must validate before opening"),
    )
    with pytest.raises(FakeYouError, match="EMBEDDING_DIM") as caught:
        async with open_graphiti_memory(config):
            pytest.fail("must reject incompatible vector dimensions")
    assert caught.value.code is ErrorCode.CONFIG_EMBEDDING_DIM_MISMATCH


def test_preflight_preserves_all_active_role_urls_and_ports() -> None:
    from fakeyou.orchestration.providers import preflight_hosts

    settings = Settings(
        **env_file_opt(None),
        reply=CompatibleReply(
            provider="openai_compatible",
            base_url="http://localhost:8001/v1",
            max_retries=0,
        ),
        reply_fallback=AnthropicReply(provider="anthropic", max_retries=0),
    )
    settings.memory.store.uri = "bolt://localhost:7688"
    settings.memory.embedder.base_url = "http://localhost:8080/v1"
    targets = preflight_hosts(settings, memory=True)
    assert "http://localhost:8001/v1" in targets
    assert "https://api.anthropic.com" in targets
    assert "bolt://localhost:7688" in targets
    assert "http://localhost:8080/v1" in targets
    assert "https://api.openai.com/v1" in targets


def test_anthropic_memory_client_uses_explicit_endpoint_and_key() -> None:
    from graphiti_core.llm_client.anthropic_client import AnthropicClient

    from fakeyou.memory.memory import AnthropicReranker, create_graphiti_memory

    config = MemorySettings.model_validate(
        {
            "store": {"uri": "bolt://localhost:7687", "user": "test", "password": "test"},
            "llm": {
                "model": "claude-haiku-4-5-20251001",
                "small_model": "claude-haiku-4-5-20251001",
                "api_key": "llm",
                "client": "anthropic",
                "base_url": "https://anthropic.example.com",
            },
            "embedder": {"model": "embed", "api_key": "embedding"},
        }
    )
    memory = create_graphiti_memory(config)
    assert isinstance(memory.llm_client, AnthropicClient)
    assert str(memory.llm_client.client.base_url) == "https://anthropic.example.com"
    assert memory.llm_client.client.api_key == "llm"
    assert isinstance(memory.cross_encoder, AnthropicReranker)


async def test_speech_language_and_explicit_voice_model_reach_provider_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import httpx2

    import fakeyou.speech.openai as openai_speech
    import fakeyou.speech.registry as registry
    from fakeyou.config.settings import MiniMaxTTS, OpenAITTS

    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return httpx2.Response(200, content=b"\x00\x01")

    monkeypatch.setattr(
        openai_speech,
        "AsyncOpenAI",
        lambda **kwargs: __import__("openai").AsyncOpenAI(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        ),
    )
    synthesizer = registry.build_speech_synthesizer(
        OpenAITTS(api_key=SecretStr("tts"), model="explicit-model", voice="nova"),
        language="en",
    )
    try:
        assert b"".join([chunk async for chunk in synthesizer.stream("Hello.")]) == b"\x00\x01"
    finally:
        await synthesizer.aclose()
    assert requests[0]["model"] == "explicit-model"
    assert requests[0]["voice"] == "nova"
    assert "Language: English." in requests[0]["instructions"]
    minimax_requests: list[dict[str, Any]] = []

    def minimax_respond(request: httpx2.Request) -> httpx2.Response:
        minimax_requests.append(json.loads(request.content))
        return httpx2.Response(200, content=b'data: {"extra_info": {}}\n\n')

    minimax_client = httpx2.AsyncClient(transport=httpx2.MockTransport(minimax_respond))
    monkeypatch.setattr(httpx2, "AsyncClient", lambda **_kwargs: minimax_client)
    minimax = registry.build_speech_synthesizer(
        MiniMaxTTS(api_key=SecretStr("tts"), model="explicit-minimax", voice="explicit-voice"),
        language="en",
    )
    try:
        [chunk async for chunk in minimax.stream("Hello.")]
    finally:
        await minimax.aclose()
    assert minimax_requests[0]["language_boost"] == "English"
    assert minimax_requests[0]["model"] == "explicit-minimax"
    assert minimax_requests[0]["voice_setting"]["voice_id"] == "explicit-voice"


async def test_anthropic_reranker_orders_scores_and_rejects_incomplete_results() -> None:
    from unittest.mock import create_autospec

    from graphiti_core.llm_client.anthropic_client import AnthropicClient

    from fakeyou.memory.memory import AnthropicReranker

    client = create_autospec(AnthropicClient, instance=True, spec_set=True)
    client.generate_response.return_value = {"scores": [0.2, 0.9]}
    reranker = AnthropicReranker(client)
    assert await reranker.rank("agenda", ["irrelevant", "relevant"]) == [
        ("relevant", 0.9),
        ("irrelevant", 0.2),
    ]
    client.generate_response.return_value = {"scores": [0.5]}
    with pytest.raises(ValueError, match="shorter than argument 1"):
        await reranker.rank("agenda", ["first", "second"])


async def test_anthropic_memory_reranker_uses_only_its_configured_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import json

    import httpx2
    from graphiti_core.embedder.openai import OpenAIEmbedder
    from graphiti_core.llm_client.anthropic_client import AnthropicClient

    import fakeyou.memory.memory as memory_module
    from fakeyou.memory.memory import GraphitiAnthropicSDK, create_graphiti_memory

    requests = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "message-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5-20251001",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "score-1",
                        "name": "RelevanceScores",
                        "input": {"scores": [0.1, 0.8]},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        )

    transport = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(
        memory_module,
        "GraphitiAnthropicSDK",
        lambda **kwargs: GraphitiAnthropicSDK(**kwargs, http_client=transport),
    )
    config = MemorySettings.model_validate(
        {
            "store": {"uri": "bolt://localhost:7687", "user": "test", "password": "test"},
            "llm": {
                "model": "claude-haiku-4-5-20251001",
                "small_model": "claude-haiku-4-5-20251001",
                "api_key": "anthropic-role-key",
                "client": "anthropic",
                "base_url": "https://custom.anthropic.test:8443",
            },
            "embedder": {"model": "embed", "api_key": "embedding-role-key"},
        }
    )
    memory = await asyncio.to_thread(create_graphiti_memory, config)
    try:
        assert await memory.cross_encoder.rank("agenda", ["other", "agenda"]) == [
            ("agenda", 0.8),
            ("other", 0.1),
        ]
    finally:
        closer: Callable[[], Coroutine[Any, Any, None]] = memory.close
        await closer()
        assert isinstance(memory.llm_client, AnthropicClient)
        await memory.llm_client.client.close()
        assert isinstance(memory.embedder, OpenAIEmbedder)
        await memory.embedder.client.close()
    assert len(requests) == 1
    assert str(requests[0].url) == "https://custom.anthropic.test:8443/v1/messages"
    assert requests[0].headers["x-api-key"] == "anthropic-role-key"
    assert json.loads(requests[0].content)["model"] == config.llm.model
    assert "embedding-role-key" not in str(requests[0].headers)
    assert json.loads(requests[0].content)["temperature"] == memory.llm_client.temperature


@pytest.mark.parametrize("role", ["reply", "reply_fallback"])
@pytest.mark.parametrize(
    ("provider", "expected"),
    [("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com")],
)
def test_blank_reply_endpoint_ignores_library_destination_environment(
    monkeypatch: pytest.MonkeyPatch, provider: str, expected: str, role: str
) -> None:
    from langchain_anthropic import ChatAnthropic
    from langchain_openai import ChatOpenAI

    from fakeyou.orchestration.providers import (
        create_chat_model,
        create_fallback_chat_model,
        preflight_hosts,
    )

    monkeypatch.setenv("OPENAI_BASE_URL", "https://unconfigured.example.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unconfigured.example.invalid")
    configuration: dict[str, Any] = {"reply": OpenAIReply(provider="openai", max_retries=0)}
    if provider == "openai":
        configuration[role] = OpenAIReply(
            provider="openai",
            base_url="",
            api_key=SecretStr("reply-role-key"),
            max_retries=0,
        )
    else:
        configuration[role] = AnthropicReply(
            provider="anthropic",
            base_url="",
            api_key=SecretStr("reply-role-key"),
            max_retries=0,
        )
    settings = Settings(**env_file_opt(None), **configuration)
    assert getattr(settings, role).base_url is None
    model = create_chat_model(settings) if role == "reply" else create_fallback_chat_model(settings)
    assert model is not None
    if provider == "openai":
        assert isinstance(model, ChatOpenAI)
        endpoint = (str(model.root_client.base_url), model.root_client.api_key)
    else:
        assert isinstance(model, ChatAnthropic)
        endpoint = (str(model.anthropic_api_url), model.anthropic_api_key.get_secret_value())
    assert (endpoint[0].rstrip("/"), endpoint[1]) == (expected, "reply-role-key")
    assert expected in preflight_hosts(settings)


def test_google_reply_uses_its_own_endpoint_key_and_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fakeyou.orchestration.providers import create_chat_model, preflight_hosts

    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://unconfigured.example.invalid")
    monkeypatch.setenv("GOOGLE_API_KEY", "unconfigured-key")
    settings = Settings(
        **env_file_opt(None),
        reply=GoogleReply(
            provider="google",
            base_url="",
            api_key=SecretStr("reply-role-key"),
            max_retries=0,
        ),
    )
    from langchain_google_genai import ChatGoogleGenerativeAI

    model = create_chat_model(settings)
    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.client is not None
    assert isinstance(model.base_url, str)
    assert model.google_api_key is not None

    assert (model.model, model.timeout, model.max_retries) == ("gemini-3.8-flash", 15.0, 0)
    assert model.base_url.rstrip("/") == "https://generativelanguage.googleapis.com"
    assert model.google_api_key.get_secret_value() == "reply-role-key"
    assert "https://generativelanguage.googleapis.com" in preflight_hosts(settings)


def test_debrief_role_defaults_to_reply_and_otherwise_uses_its_own_model_and_key() -> None:
    from fakeyou.orchestration.providers import create_debrief_chat_model

    reply = GoogleReply(
        provider="google", model="gemini-3.5-flash-lite", api_key=SecretStr("reply-key")
    )
    from langchain_google_genai import ChatGoogleGenerativeAI

    shared = Settings(**env_file_opt(None), reply=reply)
    assert shared.debrief_role is shared.reply
    shared_model = create_debrief_chat_model(shared)
    assert isinstance(shared_model, ChatGoogleGenerativeAI)
    assert shared_model.model == "gemini-3.5-flash-lite"

    debrief = GoogleReply(
        provider="google", model="gemini-3.8-flash", api_key=SecretStr("debrief-key")
    )
    model = create_debrief_chat_model(Settings(**env_file_opt(None), reply=reply, debrief=debrief))
    assert isinstance(model, ChatGoogleGenerativeAI)
    assert model.google_api_key is not None
    assert (model.model, model.google_api_key.get_secret_value()) == (
        "gemini-3.8-flash",
        "debrief-key",
    )
    with pytest.raises(FakeYouError, match="FAKEYOU_DEBRIEF__API_KEY") as caught:
        create_debrief_chat_model(
            Settings(
                **env_file_opt(None),
                reply=reply,
                debrief=AnthropicReply(provider="anthropic"),
            )
        )
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


@pytest.mark.parametrize(
    ("client", "expected"),
    [("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com")],
)
def test_blank_memory_endpoints_ignore_library_destination_environment(
    monkeypatch: pytest.MonkeyPatch, client: str, expected: str
) -> None:
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder
    from graphiti_core.llm_client.anthropic_client import AnthropicClient
    from graphiti_core.llm_client.openai_client import OpenAIClient

    from fakeyou.memory.memory import create_graphiti_memory
    from fakeyou.orchestration.providers import memory_config, preflight_hosts

    monkeypatch.setenv("OPENAI_BASE_URL", "https://unconfigured.example.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unconfigured.example.invalid")
    llm: OpenAIMemoryLLM | AnthropicMemoryLLM
    if client == "openai":
        llm = OpenAIMemoryLLM(client="openai", base_url="", api_key=SecretStr("memory-role-key"))
    else:
        llm = AnthropicMemoryLLM(
            client="anthropic", base_url="", api_key=SecretStr("memory-role-key")
        )
    settings = Settings(
        **env_file_opt(None),
        memory=MemorySettings(
            llm=llm,
            embedder=EmbedderSettings(base_url="", api_key=SecretStr("embedding-role-key")),
            store=StoreSettings(password=SecretStr("store-key")),
        ),
    )
    assert settings.memory.llm.base_url is None
    assert settings.memory.embedder.base_url is None

    config = memory_config(settings)
    assert config.llm.resolved_base_url == expected
    assert config.embedder.resolved_base_url == "https://api.openai.com/v1"
    memory = create_graphiti_memory(
        MemorySettings.model_validate(
            {
                **config.model_dump(),
                "llm": {**config.llm.model_dump(), "base_url": None},
                "embedder": {**config.embedder.model_dump(), "base_url": None},
            }
        )
    )
    if client == "openai":
        assert isinstance(memory.llm_client, OpenAIClient)
    else:
        assert isinstance(memory.llm_client, AnthropicClient)
    assert isinstance(memory.embedder, OpenAIEmbedder)
    assert str(memory.llm_client.client.base_url).rstrip("/") == expected
    assert memory.llm_client.client.api_key == "memory-role-key"
    assert str(memory.embedder.client.base_url).rstrip("/") == "https://api.openai.com/v1"
    assert memory.embedder.client.api_key == "embedding-role-key"
    if client == "openai":
        assert isinstance(memory.cross_encoder, OpenAIRerankerClient)
        assert str(memory.cross_encoder.client.base_url).rstrip("/") == expected
        assert memory.cross_encoder.client.api_key == "memory-role-key"
    targets = preflight_hosts(settings, memory=True)
    assert expected in targets
    assert "https://api.openai.com/v1" in targets


def test_history_max_tokens_defaults_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(**env_file_opt(None)).conversation.history_max_tokens == 32000
    monkeypatch.setenv("FAKEYOU_CONVERSATION__HISTORY_MAX_TOKENS", "1024")
    assert Settings(**env_file_opt(None)).conversation.history_max_tokens == 1024


def test_docker_timeout_defaults_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(**env_file_opt(None)).worker.docker_timeout_seconds == 30.0
    monkeypatch.setenv("FAKEYOU_WORKER__DOCKER_TIMEOUT_SECONDS", "7.5")
    assert Settings(**env_file_opt(None)).worker.docker_timeout_seconds == 7.5


def test_diarize_budget_defaults_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    stt = Settings(**env_file_opt(None)).stt
    assert stt.diarize_timeout_seconds == 60.0
    assert stt.diarize_max_retries == 3
    monkeypatch.setenv("FAKEYOU_STT__DIARIZE_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("FAKEYOU_STT__DIARIZE_MAX_RETRIES", "1")
    stt = Settings(**env_file_opt(None)).stt
    assert stt.diarize_timeout_seconds == 7.5
    assert stt.diarize_max_retries == 1


def test_memory_sdk_budgets_default_and_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(**env_file_opt(None))
    assert settings.memory.llm.timeout_seconds == 60.0
    assert settings.memory.llm.max_retries == 2
    assert settings.memory.embedder.timeout_seconds == 60.0
    assert settings.memory.embedder.max_retries == 2
    monkeypatch.setenv("FAKEYOU_MEMORY__LLM__CLIENT", "openai")
    monkeypatch.setenv("FAKEYOU_MEMORY__LLM__TIMEOUT_SECONDS", "11.0")
    monkeypatch.setenv("FAKEYOU_MEMORY__LLM__MAX_RETRIES", "5")
    monkeypatch.setenv("FAKEYOU_MEMORY__EMBEDDER__TIMEOUT_SECONDS", "12.0")
    monkeypatch.setenv("FAKEYOU_MEMORY__EMBEDDER__MAX_RETRIES", "6")
    settings = Settings(**env_file_opt(None))
    assert settings.memory.llm.timeout_seconds == 11.0
    assert settings.memory.llm.max_retries == 5
    assert settings.memory.embedder.timeout_seconds == 12.0
    assert settings.memory.embedder.max_retries == 6


def test_memory_close_budgets_default_and_read_env(monkeypatch: pytest.MonkeyPatch) -> None:
    memory = Settings(**env_file_opt(None)).memory
    assert memory.close_callback_seconds == 5.0
    assert memory.close_hard_seconds == 30.0
    monkeypatch.setenv("FAKEYOU_MEMORY__CLOSE_CALLBACK_SECONDS", "1.5")
    monkeypatch.setenv("FAKEYOU_MEMORY__CLOSE_HARD_SECONDS", "2.5")
    memory = Settings(**env_file_opt(None)).memory
    assert memory.close_callback_seconds == 1.5
    assert memory.close_hard_seconds == 2.5


def test_visual_max_concurrent_defaults_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(**env_file_opt(None)).visual.max_concurrent == 2
    monkeypatch.setenv("FAKEYOU_VISUAL__MAX_CONCURRENT", "4")
    assert Settings(**env_file_opt(None)).visual.max_concurrent == 4
