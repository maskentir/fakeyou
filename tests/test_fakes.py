import os
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from graphiti_core.embedder.client import EMBEDDING_DIM
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from fakeyou.config.settings import Settings
from tests._memory_fakes import FakeCrossEncoder, FakeEmbedder, FakeGraphitiLLM, make_real_graphiti


@pytest.mark.parametrize("prefix", ["FAKEYOU_", "fakeyou_", "FakeYou_"])
def test_settings_fixture_cleans_environment_and_uses_the_real_schema(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest, prefix: str
) -> None:
    monkeypatch.setenv(f"{prefix}CHAT_MODEL", "external:model")
    monkeypatch.setenv(f"{prefix}TTS_PROVIDER", "invalid-provider")
    monkeypatch.setenv(f"{prefix}SESSION_DURATION_SECONDS", "invalid-seconds")

    settings = request.getfixturevalue("settings")

    assert isinstance(settings, Settings)
    assert settings.reply.model == "model"
    assert settings.tts.provider == "openai"
    assert settings.meeting.session_duration_seconds == 7200.0
    assert not any(
        name.lower().startswith(Settings.model_config["env_prefix"].lower()) for name in os.environ
    )
    settings_any = cast(Any, settings)
    with pytest.raises(ValueError, match="no field"):
        settings_any.chat_modle = "typo:model"


@pytest.mark.parametrize(
    ("method", "args"),
    [("search", ("agenda",)), ("add_episode", ()), ("retrieve_episodes", ())],
)
async def test_graphiti_double_rejects_unknown_library_arguments(
    graphiti: MagicMock, method: str, args: tuple[Any, ...]
) -> None:
    with pytest.raises(TypeError):
        await getattr(graphiti, method)(*args, typo_argument=True)


class _ExtractionProbe(BaseModel):
    summary: str
    facts: list[str]
    score: float


async def test_fake_graphiti_llm_fills_the_response_model_deterministically() -> None:
    client = FakeGraphitiLLM()

    first = await client.generate_response(
        [Message(role="user", content="")], response_model=_ExtractionProbe
    )
    second = await client.generate_response(
        [Message(role="user", content="")], response_model=_ExtractionProbe
    )

    assert _ExtractionProbe.model_validate(first) == _ExtractionProbe(
        summary="", facts=[], score=0.0
    )
    assert first == second
    assert client.calls == ["_ExtractionProbe", "_ExtractionProbe"]


async def test_fake_embedder_returns_deterministic_full_dimension_vectors() -> None:
    embedder = FakeEmbedder()

    first = await embedder.create("quarterly budget review")
    second = await embedder.create("quarterly budget review")
    other = await embedder.create("unrelated topic")

    assert len(first) == EMBEDDING_DIM
    assert first == second
    assert first != other


async def test_fake_cross_encoder_ranks_token_overlap_first() -> None:
    encoder = FakeCrossEncoder()

    ranked = await encoder.rank("budget review", ["lunch menu plans", "the budget review moved"])

    assert ranked[0][0] == "the budget review moved"
    assert ranked[0][1] > ranked[1][1]


def test_make_real_graphiti_builds_the_production_client_without_connecting() -> None:
    memory = make_real_graphiti("bolt://127.0.0.1:9", password="harness-only")

    assert type(memory).__name__ == "Graphiti"
