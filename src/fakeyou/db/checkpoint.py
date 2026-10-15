from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import DEFAULTS

CHECKPOINT_POOL_MAX_SIZE = 4


def is_checkpoint_error(error: BaseException) -> bool:
    return isinstance(error, (psycopg.Error, TimeoutError))


def is_database_error(error: BaseException) -> bool:
    return isinstance(error, psycopg.Error)


def checkpoint_conninfo(
    url: str,
    *,
    connect_timeout_seconds: float,
    statement_timeout_seconds: float,
    keepalive_idle_seconds: float,
) -> str:
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["connect_timeout"] = str(max(1, round(connect_timeout_seconds)))
    statement = f"-c statement_timeout={max(1, int(statement_timeout_seconds * 1000))}"
    existing = params.get("options", "")
    params["options"] = f"{existing} {statement}" if existing else statement
    params["keepalives"] = "1"
    params["keepalives_idle"] = str(max(1, round(keepalive_idle_seconds)))
    query = urlencode(params, quote_via=quote)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


@asynccontextmanager
async def open_checkpointer(
    url: str,
    *,
    connect_timeout_seconds: float = DEFAULTS.database.connect_timeout_seconds,
    statement_timeout_seconds: float = DEFAULTS.database.statement_timeout_seconds,
    keepalive_idle_seconds: float = DEFAULTS.database.keepalive_idle_seconds,
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    conninfo = checkpoint_conninfo(
        url,
        connect_timeout_seconds=connect_timeout_seconds,
        statement_timeout_seconds=statement_timeout_seconds,
        keepalive_idle_seconds=keepalive_idle_seconds,
    )
    try:
        pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
            conninfo,
            min_size=1,
            max_size=CHECKPOINT_POOL_MAX_SIZE,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            check=AsyncConnectionPool.check_connection,
            timeout=connect_timeout_seconds,
        )
        async with pool:
            saver = AsyncPostgresSaver(pool)
            await saver.setup()
            yield saver
    except psycopg.Error as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
