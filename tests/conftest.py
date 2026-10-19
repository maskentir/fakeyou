import os
import subprocess
from collections.abc import AsyncIterator, Callable, Iterator, MutableMapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock
from uuid import uuid4

import httpx2
import pytest
from graphiti_core import Graphiti
from graphiti_core.embedder.client import EMBEDDING_DIM
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.base import SerializerProtocol
from neo4j import GraphDatabase
from neo4j.exceptions import DriverError, Neo4jError
from openai import AsyncOpenAI
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

import fakeyou.db.checkpoint as checkpoint_module
import tests._join_runner as cli_join
from conftest import HARNESS_POSTGRES_MODE
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import (
    BrowserSettings,
    DatabaseSettings,
    EmbedderSettings,
    IdentitySettings,
    MemorySettings,
    OpenAIMemoryLLM,
    OpenAIReply,
    OpenAITTS,
    ParticipationDeciderSettings,
    PathSettings,
    Settings,
    StoreSettings,
)
from fakeyou.db.engine import create_engine_for_url
from fakeyou.db.schema import upgrade
from fakeyou.memory.persona import OperatorIdentity
from tests._audio_fakes import FakeAudio, PCMStream
from tests._cli_support import AudioJoin, join_with_audio
from tests._container_harness import (
    NEO4J_IMAGE,
    POSTGRES_IMAGE,
    REDIS_IMAGE,
    require_docker,
    service_endpoint,
    start_service,
    stop_service,
    wait_ready,
)
from tests._fakes_common import DATABASE_URL
from tests._memory_fakes import make_graphiti, make_real_graphiti
from tests._persistence_fakes import RecordingJournal
from tests._runtime_fakes import SessionRun

TEST_DATABASE_ENV = "TEST_DATABASE_URL"
REAL_DATABASE_FIXTURE = "blank_database_url"
PRESERVE_OPERATOR_ENV = "FAKEYOU_PRESERVE_OPERATOR_ENV"


def purge_operator_environment(
    environ: MutableMapping[str, str], *, preserve: bool = False
) -> None:
    if preserve:
        return
    for name in [name for name in environ if name.upper().startswith("FAKEYOU_")]:
        del environ[name]


PRESERVE_OPERATOR_ENVIRONMENT = bool(os.environ.get(PRESERVE_OPERATOR_ENV))
purge_operator_environment(os.environ, preserve=PRESERVE_OPERATOR_ENVIRONMENT)


@pytest.fixture(autouse=True)
def _isolated_operator_environment() -> Iterator[None]:
    saved = {
        name: value for name, value in os.environ.items() if name.upper().startswith("FAKEYOU_")
    }
    purge_operator_environment(os.environ, preserve=PRESERVE_OPERATOR_ENVIRONMENT)
    try:
        yield
    finally:
        purge_operator_environment(os.environ, preserve=PRESERVE_OPERATOR_ENVIRONMENT)
        os.environ.update(saved)


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

    class FakeConnectionPool:
        def __init__(self, conninfo: str, **_kwargs: Any) -> None:
            self.conninfo = conninfo

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        @staticmethod
        async def check_connection(_connection: object) -> None:
            return None

    def fake_saver(
        pool: FakeConnectionPool, *, serde: SerializerProtocol | None = None
    ) -> MemoryPostgresSaver:
        return savers.setdefault(pool.conninfo, MemoryPostgresSaver(serde=serde))

    monkeypatch.setattr(checkpoint_module, "AsyncConnectionPool", FakeConnectionPool)
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", fake_saver)
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
            browser_profile_dir=tmp_path / "profiles",
            browser_output_dir=tmp_path / "browser",
        ),
        identity=IdentitySettings(
            owner_id=SessionId("settings-owner"),
            twin_id=SessionId("settings-twin"),
            twin_name="Settings Twin",
        ),
        browser=BrowserSettings(headless=True),
        participation_decider=ParticipationDeciderSettings(enabled=False),
        memory=MemorySettings(
            store=StoreSettings(password=SecretStr("password")),
            llm=OpenAIMemoryLLM(
                model="memory-model", small_model="memory-small", api_key=SecretStr("memory-key")
            ),
            embedder=EmbedderSettings(
                dim=EMBEDDING_DIM, model="memory-embedding", api_key=SecretStr("embedding-key")
            ),
        ),
    )


@pytest.fixture(scope="session")
def harness_postgres_url() -> Iterator[str | None]:
    external = os.environ.get(TEST_DATABASE_ENV)
    if external:
        yield external
        return
    if HARNESS_POSTGRES_MODE != "container":
        yield None
        return
    require_docker()
    name = f"fakeyou-harness-postgres-{uuid4().hex[:12]}"
    container = start_service(
        POSTGRES_IMAGE,
        name=name,
        env={
            "POSTGRES_DB": "fakeyou",
            "POSTGRES_USER": "fakeyou",
            "POSTGRES_PASSWORD": "harness-only",
        },
        container_port=5432,
    )
    try:
        endpoint = service_endpoint(container, 5432)
        url = f"postgresql://fakeyou:harness-only@{endpoint.host}:{endpoint.port}/fakeyou"

        def ready() -> bool:
            engine = create_engine(make_url(url).set(drivername="postgresql+psycopg"))
            try:
                with engine.connect() as connection:
                    connection.execute(text("SELECT 1"))
            except SQLAlchemyError:
                return False
            else:
                return True
            finally:
                engine.dispose()

        wait_ready(ready, description="harness postgres")
        yield url
    finally:
        stop_service(container)


@pytest.fixture(scope="session")
def neo4j_endpoint() -> Iterator[str]:
    require_docker()
    name = f"fakeyou-harness-neo4j-{uuid4().hex[:12]}"
    container = start_service(
        NEO4J_IMAGE,
        name=name,
        env={"NEO4J_AUTH": "neo4j/harness-only"},
        container_port=7687,
    )
    try:
        endpoint = service_endpoint(container, 7687)
        uri = f"bolt://{endpoint.host}:{endpoint.port}"

        def ready() -> bool:
            driver = GraphDatabase.driver(uri, auth=("neo4j", "harness-only"))
            try:
                driver.verify_connectivity()
            except (Neo4jError, DriverError, OSError):
                return False
            else:
                return True
            finally:
                driver.close()

        wait_ready(ready, description="harness neo4j", timeout_seconds=180.0)
        yield uri
    finally:
        stop_service(container)


@pytest.fixture(scope="session")
def redis_endpoint() -> Iterator[str]:
    require_docker()
    name = f"fakeyou-harness-redis-{uuid4().hex[:12]}"
    container = start_service(REDIS_IMAGE, name=name, env={}, container_port=6379)
    try:
        endpoint = service_endpoint(container, 6379)

        def ready() -> bool:
            probe = subprocess.run(
                ["docker", "exec", container, "redis-cli", "ping"],
                capture_output=True,
                text=True,
                check=False,
            )
            return probe.stdout.strip() == "PONG"

        wait_ready(ready, description="harness redis", timeout_seconds=60.0)
        yield f"redis://{endpoint.host}:{endpoint.port}/0"
    finally:
        stop_service(container)


@pytest.fixture
async def real_graphiti(neo4j_endpoint: str) -> AsyncIterator[Graphiti]:
    memory = make_real_graphiti(neo4j_endpoint, password="harness-only")
    await memory.build_indices_and_constraints()
    try:
        yield memory
    finally:
        await cast(Any, memory).close()


@pytest.fixture
def blank_database_url(harness_postgres_url: str | None) -> Iterator[str]:
    base_url = harness_postgres_url
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
    engine = create_engine_for_url(
        blank_database_url, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
    try:
        upgrade(engine)
    finally:
        engine.dispose()
    return blank_database_url


@pytest.fixture
def migrated_engine(migrated_database_url: str) -> Iterator[Engine]:
    engine = create_engine_for_url(
        migrated_database_url, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
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
        return await cli_join.join_in_process(
            link, *args, options=bundled, settings=request_settings
        )

    return go


@pytest.fixture
def audio_join(
    monkeypatch: pytest.MonkeyPatch, join: Callable[..., Any], settings: Settings
) -> AudioJoin:
    return partial(join_with_audio, monkeypatch, join=join, settings=settings)


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
