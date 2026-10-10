from __future__ import annotations

import pytest
from pydantic import SecretStr

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import (
    configured_url,
    create_engine_for_url,
    open_database,
    require_url,
)

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

    with pytest.raises(FakeYouError) as error:
        configured_url(settings)

    assert error.value.code is ErrorCode.DATABASE_URL_SCHEME
    assert "hunter2" not in str(error.value)


def test_require_url_returns_a_present_url() -> None:
    assert require_url(PLAIN_URL) == PLAIN_URL


def test_require_url_refuses_a_missing_url() -> None:
    with pytest.raises(FakeYouError) as error:
        require_url(None)

    assert error.value.code is ErrorCode.DATABASE_URL_MISSING


def test_create_engine_for_url_selects_the_psycopg_driver() -> None:
    engine = create_engine_for_url(PLAIN_URL)
    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_open_database_requires_a_configured_url(settings: Settings) -> None:
    settings.database.url = None

    with pytest.raises(FakeYouError) as error, open_database(settings):
        pass

    assert error.value.code is ErrorCode.DATABASE_URL_MISSING


def test_open_database_reports_an_unreachable_server_without_the_password(
    settings: Settings,
) -> None:
    settings.database.url = SecretStr("postgresql://fakeyou:secret@127.0.0.1:1/fakeyou")

    with pytest.raises(FakeYouError) as error:
        with open_database(settings) as engine, engine.connect():
            pass

    assert error.value.code is ErrorCode.DATABASE_UNREACHABLE
    assert error.value.__cause__ is not None
    assert "secret" not in str(error.value)
    assert "secret" not in str(error.value.__cause__)
