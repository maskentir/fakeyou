import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx2
import pytest
from graphiti_core.embedder.client import EMBEDDING_DIM
from openai import AsyncOpenAI
from pydantic import SecretStr

import fakeyou.cli.join as cli_join
from fakeyou.settings import (
    BrowserSettings,
    EmbedderSettings,
    IdentitySettings,
    MemorySettings,
    OpenAIMemoryLLM,
    OpenAIReply,
    OpenAITTS,
    PathSettings,
    Settings,
    StoreSettings,
)
from tests.fakes import FakeAudio, PCMStream, RecordingJournal, SessionRun, make_graphiti


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    prefix = Settings.model_config.get("env_prefix", "")
    case_sensitive = Settings.model_config.get("case_sensitive", False)
    if not case_sensitive:
        prefix = prefix.lower()
    for name in os.environ:
        candidate = name if case_sensitive else name.lower()
        if candidate.startswith(prefix):
            monkeypatch.delenv(name)
    environment_options: dict[str, Any] = {"_env_file": None}
    return Settings(
        **environment_options,
        reply=OpenAIReply(api_key=SecretStr("test-key"), model="model"),
        tts=OpenAITTS(api_key=SecretStr("tts-key")),
        paths=PathSettings(
            checkpoint_path=tmp_path / "checkpoints.sqlite",
            journal_dir=tmp_path / "journals",
            record_dir=tmp_path / "meetings",
        ),
        identity=IdentitySettings(
            owner_id="settings-owner", twin_id="settings-twin", twin_name="Settings Twin"
        ),
        browser=BrowserSettings(headless=True),
        memory=MemorySettings(
            store=StoreSettings(group="test-group", password=SecretStr("password")),
            llm=OpenAIMemoryLLM(
                model="memory-model", small_model="memory-small", api_key=SecretStr("memory-key")
            ),
            embedder=EmbedderSettings(
                dim=EMBEDDING_DIM, model="memory-embedding", api_key=SecretStr("embedding-key")
            ),
        ),
    )


@pytest.fixture
def graphiti() -> MagicMock:
    return make_graphiti()


@pytest.fixture
def journal() -> Iterator[RecordingJournal]:
    result = RecordingJournal()
    try:
        yield result
    finally:
        result.close()


@pytest.fixture
def join(settings: Settings) -> Callable[..., Any]:
    return partial(
        cli_join.join_meeting,
        twin_name=None,
        owner_id=None,
        twin_id=None,
        meeting_id="meeting-1",
        lobby_timeout=None,
        duration=None,
        profile_dir=None,
        settings=settings,
    )


@pytest.fixture
def session_run(monkeypatch: pytest.MonkeyPatch) -> SessionRun:
    run = SessionRun()
    monkeypatch.setattr(cli_join, "run_meeting_session", run)
    return run


@pytest.fixture
def open_audio(session: Any) -> Callable[..., AbstractAsyncContextManager[FakeAudio]]:
    @asynccontextmanager
    async def open_audio(
        audio: FakeAudio | None = None, *, started: bool = False
    ) -> AsyncIterator[FakeAudio]:
        if started:
            session.events.append("audio_started")
        instance = audio or FakeAudio(session.events)
        try:
            yield instance
        finally:
            await instance.stop()

    return open_audio


@pytest.fixture
async def openai_client() -> AsyncIterator[Callable[..., AsyncOpenAI]]:
    clients: list[AsyncOpenAI] = []

    def factory(
        *,
        requests: list[httpx2.Request] | None = None,
        error: Exception | None = None,
        status: int = 200,
        chunks: list[bytes] | None = None,
        stream_error: Exception | None = None,
    ) -> AsyncOpenAI:
        def handler(request: httpx2.Request) -> httpx2.Response:
            if requests is not None:
                requests.append(request)
            if error is not None:
                raise error
            return (
                httpx2.Response(status, stream=PCMStream(chunks, stream_error))
                if chunks is not None
                else httpx2.Response(status, content=b"\x00\x01" * 100)
            )

        client = AsyncOpenAI(
            api_key="test-openai-key",
            http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
            max_retries=0,
        )
        clients.append(client)
        return client

    try:
        yield factory
    finally:
        for client in clients:
            await client.close()
