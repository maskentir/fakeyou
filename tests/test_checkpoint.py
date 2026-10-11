from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from pydantic import SecretStr
from sqlalchemy import Engine, inspect

import fakeyou.db.checkpoint as checkpoint_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.diagnostics import check_setup
from fakeyou.config.settings import Settings
from fakeyou.db.checkpoint import open_checkpointer
from tests.fakes import env_file_opt

LANGGRAPH_TABLES = {"checkpoint_blobs", "checkpoint_migrations", "checkpoint_writes", "checkpoints"}


@pytest.mark.asyncio
async def test_open_checkpointer_uses_postgres_and_runs_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakePostgresSaver:
        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, conn_string: str) -> AsyncIterator["FakePostgresSaver"]:
            calls["conn_string"] = conn_string
            saver = cls()
            calls["saver"] = saver
            yield saver

        async def setup(self) -> None:
            calls["setup"] = True

    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", FakePostgresSaver)

    async with open_checkpointer("postgresql://user:pass@localhost/db") as saver:
        assert isinstance(saver, FakePostgresSaver)

    assert calls["conn_string"] == "postgresql://user:pass@localhost/db"
    assert calls["setup"] is True


@pytest.mark.asyncio
async def test_setup_creates_the_langgraph_tables_and_keeps_a_thread(
    migrated_database_url: str, migrated_engine: Engine
) -> None:
    config: RunnableConfig = {
        "configurable": {"thread_id": "checkpoint-roundtrip", "checkpoint_ns": ""}
    }
    checkpoint = empty_checkpoint()

    async with open_checkpointer(migrated_database_url) as saver:
        assert LANGGRAPH_TABLES <= set(inspect(migrated_engine).get_table_names())
        await saver.aput(config, checkpoint, {"source": "input", "step": -1, "parents": {}}, {})

    async with open_checkpointer(migrated_database_url) as saver:
        stored = await saver.aget_tuple(config)

    assert stored is not None
    assert stored.checkpoint["id"] == checkpoint["id"]


@pytest.mark.asyncio
async def test_open_checkpointer_reports_an_unreachable_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", AsyncPostgresSaver)

    with pytest.raises(FakeYouError) as raised:
        async with open_checkpointer("postgresql://fakeyou:secret@127.0.0.1:1/fakeyou"):
            pass

    assert raised.value.code is ErrorCode.DATABASE_UNREACHABLE


@pytest.mark.asyncio
async def test_open_checkpointer_maps_driver_errors_to_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingSaver:
        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, _conn_string: str) -> AsyncIterator["FailingSaver"]:
            raise psycopg.ProgrammingError("boom")
            yield cls()

    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", FailingSaver)

    with pytest.raises(FakeYouError) as raised:
        async with open_checkpointer("postgresql://localhost/db"):
            pass

    assert raised.value.code is ErrorCode.DATABASE_UNREACHABLE


def test_check_masks_the_database_url(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr("postgresql://fakeyou:s3cret@localhost/fakeyou")

    check_setup(settings)

    report = capsys.readouterr().out
    assert "s3cret" not in report
    assert "**********" in report


def test_database_url_loads_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKEYOU_DATABASE__URL", "postgresql://fakeyou:x@localhost/fakeyou")

    url = Settings(**env_file_opt(None)).database.url

    assert url is not None
    assert url.get_secret_value() == "postgresql://fakeyou:x@localhost/fakeyou"
