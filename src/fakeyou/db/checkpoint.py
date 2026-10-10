from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from fakeyou.catalog.errors import ErrorCode, FakeYouError


@asynccontextmanager
async def open_checkpointer(url: str) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    try:
        async with AsyncPostgresSaver.from_conn_string(url) as saver:
            await saver.setup()
            yield saver
    except psycopg.Error as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error
