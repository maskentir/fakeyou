from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

if TYPE_CHECKING:
    from fakeyou.settings import Settings


def checkpoint_dsn(settings: Settings) -> str | None:
    url = settings.checkpoint.url
    if url is None:
        return None
    return url.get_secret_value() or None


@asynccontextmanager
async def open_checkpointer(
    path: Path, dsn: str | None = None
) -> AsyncIterator[BaseCheckpointSaver[Any]]:
    if dsn:
        async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
            await saver.setup()
            yield saver
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver
