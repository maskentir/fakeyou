from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import SecretStr

import fakeyou.cli.db as cli_db
import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import create_engine_for_url
from fakeyou.db.schema import head_revision
from fakeyou.db.tables import metadata


@pytest.fixture
def unversioned_database_url(blank_database_url: str) -> str:
    engine = create_engine_for_url(
        blank_database_url, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    return blank_database_url


def test_db_upgrade_migrates_a_blank_database_to_head(
    settings: Settings, blank_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr(blank_database_url)

    cli_db.db_upgrade(settings=settings)

    assert json.loads(capsys.readouterr().out) == {
        "current": head_revision(),
        "head": head_revision(),
        "differences": [],
    }


def test_db_status_reports_no_current_revision_on_a_blank_database(
    settings: Settings, blank_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr(blank_database_url)

    cli_db.db_status(settings=settings)

    report = json.loads(capsys.readouterr().out)
    assert report["current"] is None
    assert report["head"] == head_revision()
    assert report["differences"]


def test_db_status_reports_the_head_on_a_migrated_database(
    settings: Settings, migrated_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr(migrated_database_url)

    cli_db.db_status(settings=settings)

    assert json.loads(capsys.readouterr().out) == {
        "current": head_revision(),
        "head": head_revision(),
        "differences": [],
    }


def test_db_stamp_adopts_matching_tables_and_prints_head(
    settings: Settings, unversioned_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr(unversioned_database_url)

    cli_db.db_stamp(settings=settings)

    assert json.loads(capsys.readouterr().out) == {
        "current": head_revision(),
        "head": head_revision(),
        "differences": [],
    }


def test_db_stamp_refuses_a_database_without_the_voiceprints_table(
    settings: Settings, blank_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings.database.url = SecretStr(blank_database_url)

    with pytest.raises(FakeYouError, match="refusing to record it as applied") as error:
        cli_db.db_stamp(settings=settings)

    assert error.value.code is ErrorCode.DATABASE_STAMP_REFUSED

    cli_db.db_status(settings=settings)

    assert json.loads(capsys.readouterr().out)["current"] is None


@pytest.mark.parametrize(
    "handler",
    [cli_db.db_upgrade, cli_db.db_stamp, cli_db.db_status],
    ids=["upgrade", "stamp", "status"],
)
def test_db_commands_refuse_to_run_without_a_database_url(
    settings: Settings, handler: Callable[..., None]
) -> None:
    settings.database.url = None

    with pytest.raises(FakeYouError, match="FAKEYOU_DATABASE__URL") as error:
        handler(settings=settings)

    assert error.value.code is ErrorCode.DATABASE_URL_MISSING


@pytest.mark.parametrize(
    ("command", "handler"),
    [
        ("upgrade", cli_db.db_upgrade),
        ("stamp", cli_db.db_stamp),
        ("status", cli_db.db_status),
    ],
)
def test_db_commands_are_registered_on_the_cli(
    command: str, handler: Callable[..., None], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_command(*args: Any, **kwargs: Any) -> None:
        assert args == ()
        calls.append(kwargs)

    monkeypatch.setattr(cli_main, handler.__name__, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "db", command])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    [call] = calls
    assert set(call) == {"settings"}
