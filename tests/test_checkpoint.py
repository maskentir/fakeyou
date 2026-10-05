from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import SecretStr

import fakeyou.checkpoint as checkpoint_module
from fakeyou.checkpoint import checkpoint_dsn, open_checkpointer
from fakeyou.cli.diagnostics import check_setup
from fakeyou.settings import Settings


@pytest.mark.asyncio
async def test_open_checkpointer_stays_sqlite_without_a_dsn(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "checkpoints.sqlite"

    async with open_checkpointer(path) as saver:
        assert isinstance(saver, AsyncSqliteSaver)

    assert path.exists()


@pytest.mark.asyncio
async def test_open_checkpointer_uses_postgres_when_a_dsn_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    class FakePostgresSaver:
        @classmethod
        @asynccontextmanager
        async def from_conn_string(cls, conn_string: str):
            calls["conn_string"] = conn_string
            saver = cls()
            calls["saver"] = saver
            yield saver

        async def setup(self) -> None:
            calls["setup"] = True

    monkeypatch.setattr(checkpoint_module, "AsyncPostgresSaver", FakePostgresSaver)

    async with open_checkpointer(
        tmp_path / "checkpoints.sqlite", dsn="postgresql://user:pass@localhost/db"
    ) as saver:
        assert isinstance(saver, FakePostgresSaver)

    assert calls["conn_string"] == "postgresql://user:pass@localhost/db"
    assert calls["setup"] is True
    assert not (tmp_path / "checkpoints.sqlite").exists()


def test_checkpoint_dsn_treats_missing_and_blank_urls_as_unset(settings: Settings) -> None:
    assert checkpoint_dsn(settings) is None

    settings.checkpoint.url = SecretStr("")
    assert checkpoint_dsn(settings) is None

    settings.checkpoint.url = SecretStr("postgresql://u:p@localhost/db")
    assert checkpoint_dsn(settings) == "postgresql://u:p@localhost/db"


@pytest.mark.asyncio
async def test_open_checkpointer_treats_a_blank_dsn_as_sqlite(tmp_path: Path) -> None:
    async with open_checkpointer(tmp_path / "checkpoints.sqlite", dsn="") as saver:
        assert isinstance(saver, AsyncSqliteSaver)


def test_check_masks_the_checkpoint_dsn(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.checkpoint.url = SecretStr("postgresql://fakeyou:s3cret@localhost/fakeyou")

    check_setup(settings)

    report = capsys.readouterr().out
    assert "s3cret" not in report
    assert "**********" in report


def test_checkpoint_dsn_loads_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKEYOU_CHECKPOINT__URL", "postgresql://fakeyou:x@localhost/fakeyou")

    assert (
        Settings(_env_file=None).checkpoint.url is not None
        and Settings(_env_file=None).checkpoint.url.get_secret_value()
        == "postgresql://fakeyou:x@localhost/fakeyou"
    )
