from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from tests._memory_fakes import make_graphiti


@asynccontextmanager
async def tracked_memory(opened: list[str]) -> AsyncIterator[Any]:
    opened.append("open")
    yield make_graphiti()
    opened.append("close")
