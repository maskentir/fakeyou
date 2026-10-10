from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.settings import Settings

POSTGRES_SCHEME = "postgresql://"
DRIVER_PREFIX = "postgresql+psycopg://"


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


def create_engine_for_url(url: str) -> Engine:
    return create_engine(DRIVER_PREFIX + url.removeprefix(POSTGRES_SCHEME), pool_pre_ping=True)


@contextmanager
def open_database(settings: Settings) -> Iterator[Engine]:
    engine = create_engine_for_url(require_url(configured_url(settings)))
    try:
        yield engine
    except OperationalError as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
    finally:
        engine.dispose()
