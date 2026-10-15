import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
import fakeyou.orchestration.providers as providers
from fakeyou.cli.memory import CorrectionTarget
from fakeyou.config.settings import EmbedderSettings, Settings
from fakeyou.memory.persona import OperatorIdentity


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["memory-add", "A note.", "--persona", "p.json", "--owner-id", "o", "--twin-id", "t"],
            "add_owner_note",
            {"identity": OperatorIdentity(Path("p.json"), "o", "t")},
        ),
        (
            ["memory-forget", "--meeting-id", "m1", "--persona", "p.json"],
            "forget_memory",
            {
                "identity": OperatorIdentity(Path("p.json"), None, None),
                "meeting_id": "m1",
                "episode_uuid": None,
            },
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


def test_memory_add_parses_optional_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        del args
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-add",
            "A note.",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "b322d326-37b5-482a-904c-ef982f4dc510",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["note_id"] == UUID("b322d326-37b5-482a-904c-ef982f4dc510")


def test_memory_add_defaults_source_and_note_id_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        del args
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "A note.", "--persona", "p.json"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["source_at"] is None
    assert captured["note_id"] is None


def test_memory_correct_parses_target_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        del args
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "correct_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-correct",
            "Fixed note.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "a1b2c3d4-0000-4000-8000-000000000002",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["text"] == "Fixed note."
    assert captured["target"] == CorrectionTarget(
        "a1b2c3d4-0000-4000-8000-000000000001",
        UUID("a1b2c3d4-0000-4000-8000-000000000002"),
    )
    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["identity"].persona_path == Path("p.json")


@pytest.mark.parametrize(
    "argv",
    [
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "2026-09-30T06:40:00"],
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "not-a-timestamp"],
        ["memory-add", "A note.", "--persona", "p.json", "--note-id", "not-a-uuid"],
        [
            "memory-correct",
            "Fixed.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00",
        ],
    ],
)
def test_memory_commands_reject_invalid_source_or_note_id_at_parse_time(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 2


def test_memory_forget_rejects_both_targets_at_parse_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "memory-forget", "--meeting-id", "m1", "--episode", "e1", "--persona", "p"],
    )
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
