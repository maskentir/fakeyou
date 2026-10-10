import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import httpx2
import pytest
from graphiti_core.embedder.client import EMBEDDING_DIM
from langgraph.checkpoint.memory import InMemorySaver
from openai import AsyncOpenAI
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

import fakeyou.cli.join as cli_join
import fakeyou.db.checkpoint as checkpoint_module
from fakeyou.cli._operator import OperatorIdentity
from fakeyou.config.settings import (
    BrowserSettings,
    DatabaseSettings,
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
from fakeyou.db.engine import create_engine_for_url
from fakeyou.db.schema import upgrade
from tests.fakes import (
    DATABASE_URL,
    FakeAudio,
    PCMStream,
    RecordingJournal,
    SessionRun,
    make_graphiti,
)

TEST_DATABASE_ENV = "TEST_DATABASE_URL"
REAL_DATABASE_FIXTURE = "blank_database_url"


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if isinstance(item, pytest.Function) and REAL_DATABASE_FIXTURE in item.fixturenames:
            item.add_marker(pytest.mark.postgres)


class MemoryPostgresSaver(InMemorySaver):
    async def setup(self) -> None:
        return None


@pytest.fixture(autouse=True)
def postgres_checkpoints(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, MemoryPostgresSaver]]:
    savers: dict[str, MemoryPostgresSaver] = {}
    real_database = REAL_DATABASE_FIXTURE in request.fixturenames
    if real_database or request.node.get_closest_marker("container"):
        yield savers
        return

    class FakeAsyncPostgresSaver:
        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, conn_string: str) -> AsyncIterator[MemoryPostgresSaver]:
            yield savers.setdefault(conn_string, MemoryPostgresSaver())

    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", FakeAsyncPostgresSaver)
    yield savers


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
        database=DatabaseSettings(url=SecretStr(DATABASE_URL)),
        paths=PathSettings(
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
def blank_database_url() -> Iterator[str]:
    base_url = os.environ.get(TEST_DATABASE_ENV)
    if not base_url:
        pytest.skip(f"set {TEST_DATABASE_ENV} to run tests against a real PostgreSQL")
    name = f"fakeyou_test_{uuid4().hex}"
    admin = create_engine(
        make_url(base_url).set(drivername="postgresql+psycopg", database="postgres"),
        isolation_level="AUTOCOMMIT",
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield (
            make_url(base_url)
            .set(drivername="postgresql", database=name)
            .render_as_string(hide_password=False)
        )
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def migrated_database_url(blank_database_url: str) -> str:
    engine = create_engine_for_url(blank_database_url)
    try:
        upgrade(engine)
    finally:
        engine.dispose()
    return blank_database_url


@pytest.fixture
def migrated_engine(migrated_database_url: str) -> Iterator[Engine]:
    engine = create_engine_for_url(migrated_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def migrated_settings(settings: Settings, migrated_database_url: str) -> Settings:
    settings.database.url = SecretStr(migrated_database_url)
    return settings


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
    async def go(link: str, *args: Any, **kwargs: Any) -> Any:
        options: dict[str, Any] = {
            "twin_name": None,
            "meeting_id": "meeting-1",
            "lobby_timeout": None,
            "duration": None,
            "profile_dir": None,
            "settings": settings,
        }
        options.update(kwargs)
        if "identity" not in options:
            options["identity"] = OperatorIdentity(
                options.pop("persona_path", None),
                options.pop("owner_id", None),
                options.pop("twin_id", None),
            )
        request_settings = options.pop("settings")
        bundled = cli_join.JoinOptions(
            identity=options.pop("identity"),
            twin_name=options.pop("twin_name"),
            meeting_id=options.pop("meeting_id"),
            lobby_timeout=options.pop("lobby_timeout"),
            duration=options.pop("duration"),
            profile_dir=options.pop("profile_dir"),
            audio_enabled=options.pop("audio_enabled", False),
            initial_speech=options.pop("initial_speech", None),
            headless=options.pop("headless", None),
            memory=options.pop("memory", False),
            conversation_enabled=options.pop("conversation_enabled", False),
            wake_phrase=options.pop("wake_phrase", None),
            barge_in=options.pop("barge_in", False),
            brief_path=options.pop("brief_path", None),
            resume=options.pop("resume", False),
        )
        assert not options, f"unknown join kwargs: {sorted(options)}"
        return await cli_join.join_meeting(link, *args, options=bundled, settings=request_settings)

    return go


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
