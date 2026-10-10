import json
import os

import pytest
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import MemorySettings, Settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for key in os.environ:
        if key.upper().startswith("FAKEYOU_"):
            monkeypatch.delenv(key)


@pytest.mark.parametrize("name", ["FAKEYOU_RELPY", "fakeyou_relpy", "FAKEYOU_REPLY__TYPO"])
def test_unknown_process_keys_fail_even_when_empty(monkeypatch, name):
    monkeypatch.setenv(name, "")
    with pytest.raises(FakeYouError, match="(?i)(relpy|typo)"):
        Settings(_env_file=None)


def test_unknown_key_is_rejected_without_its_value(monkeypatch):
    monkeypatch.setenv("FAKEYOU_CHAT_API_KEY", "never-print-me")
    with pytest.raises(FakeYouError) as error:
        Settings(_env_file=None)
    assert error.value.code is ErrorCode.CONFIG_UNKNOWN_KEY
    assert str(error.value) == "Unknown configuration key: FAKEYOU_CHAT_API_KEY"
    assert "never-print-me" not in str(error.value)


def test_reply_budget_includes_retries_fallback_and_recall():
    with pytest.raises(ValueError, match="budget"):
        Settings(
            _env_file=None, reply={"provider": "openai", "timeout_seconds": 30, "max_retries": 2}
        )


@pytest.mark.parametrize(
    "name", ["FAKEYOU_RELPY", "FAKEYOU_REPLY__TYPO", "fakeyou_memory__embedder__dmi"]
)
def test_unknown_dotenv_keys_fail_even_when_empty(tmp_path, name):
    path = tmp_path / ".env"
    path.write_text(f"{name}=\n")
    with pytest.raises(FakeYouError, match="(?i)(relpy|typo|dmi)"):
        Settings(_env_file=path)


def test_compose_and_unrelated_library_keys_are_allowed_in_both_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("fakeyou_compose_worker_uri", "ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated")
    path = tmp_path / ".env"
    path.write_text("FAKEYOU_COMPOSE_EMBEDDINGS_IMAGE=example\nLIBRARY_OPTION=value\n")
    settings = Settings(_env_file=path)
    assert settings.reply.api_key is None


@pytest.mark.parametrize(
    "role", ["reply", "reply_fallback", "tts", "stt", "memory.llm", "memory.embedder"]
)
def test_activated_role_never_borrows_a_key(role):
    from fakeyou.config.settings import OpenAIReply

    settings = Settings(
        _env_file=None, reply_fallback=OpenAIReply(max_retries=0), reply=OpenAIReply(max_retries=0)
    )
    settings.stt.api_key = SecretStr("other-role") if role != "stt" else None
    with pytest.raises(FakeYouError, match="API_KEY") as caught:
        settings.require_roles(role)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


def test_inspection_does_not_require_credentials(capsys):
    import json

    from fakeyou.cli.diagnostics import check_setup

    settings = Settings(_env_file=None)
    check_setup(settings)
    assert json.loads(capsys.readouterr().out) == settings.model_dump(mode="json")


_REPLY_MATCH = {
    '{"model": "custom", "provider": "openai_compatible"}': "Field required",
    '{"model": "openai:gpt-4.1-mini", "provider": "openai"}': (
        "must not contain a provider prefix"
    ),
    '{"extra_body": {}, "provider": "anthropic"}': "Extra inputs are not permitted",
    '{"provider": "anthropic", "reasoning_effort": "minimal"}': ("Extra inputs are not permitted"),
    '{"extra_body": {}, "provider": "google"}': "Extra inputs are not permitted",
    '{"provider": "google", "reasoning_effort": "unlimited"}': (
        "Input should be 'minimal', 'low', 'medium' or 'high'"
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
def test_invalid_provider_configuration_is_rejected(reply):
    with pytest.raises(ValueError, match=_REPLY_MATCH[json.dumps(reply, sort_keys=True)]):
        Settings(_env_file=None, reply=reply)


def test_google_reply_accepts_reasoning_effort_with_unchanged_default():
    from fakeyou.config.settings import GoogleReply

    assert GoogleReply().reasoning_effort is None
    assert GoogleReply(reasoning_effort="low").reasoning_effort == "low"


@pytest.mark.parametrize(
    "tts",
    [{"provider": "openai", "voice": "unknown"}, {"provider": "minimax", "sample_rate": 16000}],
)
def test_invalid_voice_or_playback_rate_is_rejected(tts):
    expected = (
        "unsupported OpenAI voice"
        if tts["provider"] == "openai"
        else "TTS sample_rate must equal playback sample rate"
    )
    with pytest.raises(ValueError, match=expected):
        Settings(_env_file=None, tts=tts)


def test_fallback_and_recall_are_included_in_budget():
    with pytest.raises(ValueError, match="budget"):
        Settings(_env_file=None, reply_fallback={"provider": "openai", "max_retries": 0})
    settings = Settings(
        _env_file=None,
        reply={"provider": "openai", "max_retries": 0},
        reply_fallback={"provider": "openai", "max_retries": 1},
    )
    assert settings.conversation.response_timeout_seconds == 60


def test_empty_embedding_url_is_normalized_at_schema_boundary(monkeypatch):
    monkeypatch.setenv("FAKEYOU_MEMORY__EMBEDDER__BASE_URL", "")
    assert Settings(_env_file=None).memory.embedder.base_url is None


async def test_memory_rejects_dimension_different_from_imported_graphiti(monkeypatch):
    import fakeyou.memory.memory as module
    from fakeyou.memory.memory import open_graphiti_memory

    config = MemorySettings.model_validate(
        {
            "store": {"uri": "bolt://localhost:7687", "user": "test", "password": "test"},
            "llm": {"client": "openai", "model": "llm", "small_model": "small", "api_key": "llm"},
            "embedder": {"model": "embed", "api_key": "embedding", "dim": module.EMBEDDING_DIM + 1},
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


def test_preflight_preserves_all_active_role_urls_and_ports():
    from fakeyou.orchestration.providers import preflight_hosts

    settings = Settings(
        _env_file=None,
        reply={
            "provider": "openai_compatible",
            "base_url": "http://localhost:8001/v1",
            "max_retries": 0,
        },
        reply_fallback={"provider": "anthropic", "max_retries": 0},
    )
    settings.memory.store.uri = "bolt://localhost:7688"
    settings.memory.embedder.base_url = "http://localhost:8080/v1"
    targets = preflight_hosts(settings, memory=True)
    assert "http://localhost:8001/v1" in targets
    assert "https://api.anthropic.com" in targets
    assert "bolt://localhost:7688" in targets
    assert "http://localhost:8080/v1" in targets
    assert "https://api.openai.com/v1" in targets


def test_anthropic_memory_client_uses_explicit_endpoint_and_key():
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


async def test_speech_language_and_explicit_voice_model_reach_provider_payloads(monkeypatch):
    import json

    import httpx2

    import fakeyou.audio.voice as voice
    from fakeyou.config.settings import MiniMaxTTS, OpenAITTS

    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, content=b"\x00\x01")

    monkeypatch.setattr(
        voice,
        "AsyncOpenAI",
        lambda **kwargs: __import__("openai").AsyncOpenAI(
            **kwargs, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
        ),
    )
    synthesizer = voice.create_speech_synthesizer(
        OpenAITTS(api_key="tts", model="explicit-model", voice="nova"),
        language="en",
    )
    try:
        assert b"".join([chunk async for chunk in synthesizer.stream("Hello.")]) == b"\x00\x01"
    finally:
        await synthesizer.aclose()
    assert requests[0]["model"] == "explicit-model"
    assert requests[0]["voice"] == "nova"
    assert "Language: English." in requests[0]["instructions"]
    synthesizer = voice.create_speech_synthesizer(
        MiniMaxTTS(api_key="tts", model="explicit-minimax", voice="explicit-voice"),
        language="en",
    )
    try:
        payload = synthesizer._payload("Hello.")
    finally:
        await synthesizer.aclose()
    assert payload["language_boost"] == "English"
    assert payload["model"] == "explicit-minimax"
    assert payload["voice_setting"]["voice_id"] == "explicit-voice"


async def test_anthropic_reranker_orders_scores_and_rejects_incomplete_results():
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


async def test_anthropic_memory_reranker_uses_only_its_configured_transport(monkeypatch):
    import asyncio
    import json

    import httpx2

    import fakeyou.memory.memory as memory_module
    from fakeyou.memory.memory import GraphitiAnthropicSDK, create_graphiti_memory

    requests = []

    def respond(request):
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
        await memory.close()
        await memory.llm_client.client.close()
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
    monkeypatch, provider, expected, role
):
    from fakeyou.orchestration.providers import (
        create_chat_model,
        create_fallback_chat_model,
        preflight_hosts,
    )

    monkeypatch.setenv("OPENAI_BASE_URL", "https://unconfigured.example.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unconfigured.example.invalid")
    configuration = {"reply": {"provider": "openai", "max_retries": 0}}
    configuration[role] = {
        "provider": provider,
        "base_url": "",
        "api_key": "reply-role-key",
        "max_retries": 0,
    }
    settings = Settings(_env_file=None, **configuration)
    assert getattr(settings, role).base_url is None
    model = create_chat_model(settings) if role == "reply" else create_fallback_chat_model(settings)
    client = model.root_client if provider == "openai" else model._client
    assert str(client.base_url).rstrip("/") == expected
    assert client.api_key == "reply-role-key"
    assert expected in preflight_hosts(settings)


def test_google_reply_uses_its_own_endpoint_key_and_retry_budget(monkeypatch):
    from fakeyou.orchestration.providers import create_chat_model, preflight_hosts

    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://unconfigured.example.invalid")
    monkeypatch.setenv("GOOGLE_API_KEY", "unconfigured-key")
    settings = Settings(
        _env_file=None,
        reply={"provider": "google", "base_url": "", "api_key": "reply-role-key", "max_retries": 0},
    )
    model = create_chat_model(settings)
    client = model.client._api_client

    assert (model.model, model.timeout, model.max_retries) == ("gemini-3.8-flash", 15.0, 0)
    assert client._http_options.base_url.rstrip("/") == "https://generativelanguage.googleapis.com"
    assert client.api_key == "reply-role-key"
    assert "https://generativelanguage.googleapis.com" in preflight_hosts(settings)


def test_debrief_role_defaults_to_reply_and_otherwise_uses_its_own_model_and_key():
    from fakeyou.orchestration.providers import create_debrief_chat_model

    reply = {"provider": "google", "model": "gemini-3.5-flash-lite", "api_key": "reply-key"}
    shared = Settings(_env_file=None, reply=reply)
    assert shared.debrief_role is shared.reply
    assert create_debrief_chat_model(shared).model == "gemini-3.5-flash-lite"

    debrief = {"provider": "google", "model": "gemini-3.8-flash", "api_key": "debrief-key"}
    model = create_debrief_chat_model(Settings(_env_file=None, reply=reply, debrief=debrief))
    assert (model.model, model.google_api_key.get_secret_value()) == (
        "gemini-3.8-flash",
        "debrief-key",
    )
    with pytest.raises(FakeYouError, match="FAKEYOU_DEBRIEF__API_KEY") as caught:
        create_debrief_chat_model(
            Settings(_env_file=None, reply=reply, debrief={"provider": "anthropic"})
        )
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


@pytest.mark.parametrize(
    ("client", "expected"),
    [("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com")],
)
def test_blank_memory_endpoints_ignore_library_destination_environment(
    monkeypatch, client, expected
):
    from fakeyou.memory.memory import create_graphiti_memory
    from fakeyou.orchestration.providers import memory_config, preflight_hosts

    monkeypatch.setenv("OPENAI_BASE_URL", "https://unconfigured.example.invalid/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unconfigured.example.invalid")
    settings = Settings(
        _env_file=None,
        memory={
            "llm": {"client": client, "base_url": "", "api_key": "memory-role-key"},
            "embedder": {"base_url": "", "api_key": "embedding-role-key"},
            "store": {"password": "store-key"},
        },
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
    assert str(memory.llm_client.client.base_url).rstrip("/") == expected
    assert memory.llm_client.client.api_key == "memory-role-key"
    assert str(memory.embedder.client.base_url).rstrip("/") == "https://api.openai.com/v1"
    assert memory.embedder.client.api_key == "embedding-role-key"
    if client == "openai":
        assert str(memory.cross_encoder.client.base_url).rstrip("/") == expected
        assert memory.cross_encoder.client.api_key == "memory-role-key"
    targets = preflight_hosts(settings, memory=True)
    assert expected in targets
    assert "https://api.openai.com/v1" in targets
