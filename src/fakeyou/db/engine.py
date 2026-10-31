from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings

POSTGRES_SCHEME = "postgresql://"
DRIVER_PREFIX = "postgresql+psycopg://"
ASYNC_DRIVER_PREFIX = "postgresql+psycopg_async://"


def configured_url(settings: Settings) -> str | None:
    if settings.database.url is None:
        return None
    url = settings.database.url.get_secret_value()
    if not url:
        return None
    if url.startswith(DRIVER_PREFIX):
        return POSTGRES_SCHEME + url.removeprefix(DRIVER_PREFIX)
    if not url.startswith(POSTGRES_SCHEME):
        raise FakeYouError(ErrorCode.DATABASE_URL_SCHEME)
    return url


def require_url(url: str | None) -> str:
    if url is None:
        raise FakeYouError(ErrorCode.DATABASE_URL_MISSING)
    return url


def engine_connect_args(
    *, connect_timeout_seconds: float, statement_timeout_seconds: float
) -> dict[str, object]:
    return {
        "connect_timeout": max(1, round(connect_timeout_seconds)),
        "options": f"-c statement_timeout={max(1, int(statement_timeout_seconds * 1000))}",
    }


def create_engine_for_url(
    url: str, *, connect_timeout_seconds: float, statement_timeout_seconds: float
) -> Engine:
    return create_engine(
        DRIVER_PREFIX + url.removeprefix(POSTGRES_SCHEME),
        pool_pre_ping=True,
        connect_args=engine_connect_args(
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
        ),
    )


def create_async_engine_for_url(
    url: str, *, connect_timeout_seconds: float, statement_timeout_seconds: float
) -> AsyncEngine:
    return create_async_engine(
        ASYNC_DRIVER_PREFIX + url.removeprefix(POSTGRES_SCHEME),
        pool_pre_ping=True,
        connect_args=engine_connect_args(
            connect_timeout_seconds=connect_timeout_seconds,
            statement_timeout_seconds=statement_timeout_seconds,
        ),
    )


@contextmanager
def open_database(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_for_url(
        require_url(configured_url(settings)),
        connect_timeout_seconds=settings.database.connect_timeout_seconds,
        statement_timeout_seconds=settings.database.statement_timeout_seconds,
    )
    try:
        yield engine
    except (OperationalError, DisconnectionError) as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
    finally:
        engine.dispose()
