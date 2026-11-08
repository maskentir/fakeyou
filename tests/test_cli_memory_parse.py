import sys
from typing import Any

import pytest
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
import fakeyou.orchestration.providers as providers
from fakeyou.config.settings import EmbedderSettings, Settings


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["memory-forget", "--meeting-id", "m1"],
            "forget_meeting_memory",
            {"meeting_id": "m1", "keep_record": False},
        ),
    ],
)
def test_memory_operator_commands_parse_arguments(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, Any]
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        del args
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert {key: captured[key] for key in expected} == expected


@pytest.mark.parametrize(
    "argv",
    [
        ["memory-forget"],
        ["memory-forget", "--meeting-id", "m1", "--episode", "e1"],
        ["memory-add", "A note.", "--twin-id", "t"],
        ["memory-correct", "Fixed.", "--episode", "e1", "--twin-id", "t"],
    ],
)
def test_removed_memory_targets_fail_at_parse_time(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2


def test_memory_config_treats_a_blank_embedding_endpoint_as_the_default(settings: Settings) -> None:
    settings.memory.embedder = EmbedderSettings(base_url="", api_key=SecretStr("local"))
    assert settings.memory.embedder.base_url is None
    assert (
        providers.memory_config(settings).embedder.resolved_base_url == "https://api.openai.com/v1"
    )


def test_memory_config_routes_embeddings_and_extraction_separately(settings: Settings) -> None:
    settings.memory.embedder.base_url = "http://embeddings/v1"
    settings.memory.embedder.api_key = SecretStr("local")
    settings.memory.embedder.dim = 768
    settings.memory.llm.base_url = "https://api.meta.ai/v1"
    settings.memory.llm.api_key = SecretStr("meta-key")

    config = providers.memory_config(settings)
    assert config.embedder.api_key is not None
    assert config.llm.api_key is not None

    assert (
        config.embedder.resolved_base_url,
        config.embedder.api_key.get_secret_value(),
        config.embedder.dim,
    ) == (
        "http://embeddings/v1",
        "local",
        768,
    )
    assert (config.llm.resolved_base_url, config.llm.api_key.get_secret_value()) == (
        "https://api.meta.ai/v1",
        "meta-key",
    )
