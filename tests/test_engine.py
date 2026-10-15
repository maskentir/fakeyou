from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy.exc import DisconnectionError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import (
    configured_url,
    create_engine_for_url,
    engine_connect_args,
    open_database,
    require_url,
)
from tests._error_support import message_pattern

PLAIN_URL = "postgresql://fakeyou:pw@db.internal:5432/fakeyou"
DRIVER_URL = "postgresql+psycopg://fakeyou:pw@db.internal:5432/fakeyou"


@pytest.mark.parametrize("url", [None, SecretStr("")], ids=["unset", "blank"])
def test_configured_url_is_none_when_unset_or_blank(
    settings: Settings, url: SecretStr | None
) -> None:
    settings.database.url = url

    assert configured_url(settings) is None


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(PLAIN_URL, PLAIN_URL), (DRIVER_URL, PLAIN_URL)],
    ids=["postgresql", "psycopg-driver"],
)
def test_configured_url_normalizes_to_the_plain_postgresql_scheme(
    settings: Settings, configured: str, expected: str
) -> None:
    settings.database.url = SecretStr(configured)

    assert configured_url(settings) == expected


@pytest.mark.parametrize(
    "configured",
    [
        "mysql://fakeyou:hunter2@db.internal:3306/fakeyou",
        "postgres://fakeyou:hunter2@db.internal:5432/fakeyou",
        "postgresql+asyncpg://fakeyou:hunter2@db.internal:5432/fakeyou",
        "mongodb+srv://fakeyou:hunter2@db.internal/fakeyou",
    ],
)
def test_configured_url_refuses_other_schemes_without_echoing_the_password(
    settings: Settings, configured: str
) -> None:
    settings.database.url = SecretStr(configured)

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_URL_SCHEME),
    ) as error:
        configured_url(settings)

    assert error.value.code is ErrorCode.DATABASE_URL_SCHEME
    assert "hunter2" not in str(error.value)


def test_require_url_returns_a_present_url() -> None:
    assert require_url(PLAIN_URL) == PLAIN_URL


def test_require_url_refuses_a_missing_url() -> None:
    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_URL_MISSING),
    ) as error:
        require_url(None)

    assert error.value.code is ErrorCode.DATABASE_URL_MISSING


def test_create_engine_for_url_selects_the_psycopg_driver() -> None:
    engine = create_engine_for_url(
        PLAIN_URL, connect_timeout_seconds=5.0, statement_timeout_seconds=30.0
    )
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_engine_connect_args_carry_timeouts_to_the_driver() -> None:
    args = engine_connect_args(connect_timeout_seconds=7.0, statement_timeout_seconds=9.0)
    assert args == {
        "connect_timeout": 7,
        "options": "-c statement_timeout=9000",
    }
    assert type(args["connect_timeout"]) is int


def test_engine_connect_args_floor_a_sub_millisecond_statement_timeout() -> None:
    args = engine_connect_args(connect_timeout_seconds=7.0, statement_timeout_seconds=0.0004)

    assert args["options"] == "-c statement_timeout=1"


def test_open_database_applies_the_settings_timeouts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.database.url = SecretStr(PLAIN_URL)
    settings.database.connect_timeout_seconds = 2.0
    settings.database.statement_timeout_seconds = 4.0
    seen: dict[str, object] = {}
    real_create = create_engine_for_url

    def capture(
        url: str, *, connect_timeout_seconds: float, statement_timeout_seconds: float
    ) -> object:
        seen["connect_timeout_seconds"] = connect_timeout_seconds
        seen["statement_timeout_seconds"] = statement_timeout_seconds
        return real_create(
            url,
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
        )

    monkeypatch.setattr("fakeyou.db.engine.create_engine_for_url", capture)
    with open_database(settings):
        pass

    assert seen == {"connect_timeout_seconds": 2.0, "statement_timeout_seconds": 4.0}


def test_open_database_requires_a_configured_url(settings: Settings) -> None:
    settings.database.url = None

    with (
        pytest.raises(
            FakeYouError,
            match=message_pattern(ErrorCode.DATABASE_URL_MISSING),
        ) as error,
        open_database(settings),
    ):
        pass

    assert error.value.code is ErrorCode.DATABASE_URL_MISSING


def test_open_database_reports_an_unreachable_server_without_the_password(
    settings: Settings,
) -> None:
    settings.database.url = SecretStr("postgresql://fakeyou:secret@127.0.0.1:1/fakeyou")

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as error:
        with open_database(settings) as engine, engine.connect():
            pass

    assert error.value.code is ErrorCode.DATABASE_UNREACHABLE
    assert error.value.__cause__ is not None
    assert "secret" not in str(error.value)
    assert "secret" not in str(error.value.__cause__)


def test_open_database_maps_a_dropped_connection_to_unreachable(settings: Settings) -> None:
    settings.database.url = SecretStr("postgresql://fakeyou:secret@127.0.0.1:1/fakeyou")

    with pytest.raises(
        FakeYouError,
        match=message_pattern(ErrorCode.DATABASE_UNREACHABLE),
    ) as error:
        with open_database(settings):
            raise DisconnectionError("server closed the connection")

    assert error.value.code is ErrorCode.DATABASE_UNREACHABLE
