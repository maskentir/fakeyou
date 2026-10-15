from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from graphiti_core import Graphiti

from fakeyou.config.settings import Settings
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile, load_operator_persona


@dataclass(frozen=True, slots=True)
class OperatorAccess:
    identity: OperatorIdentity
    settings: Settings
    open_memory: Callable[[], AbstractAsyncContextManager[Graphiti]]


@asynccontextmanager
async def open_operator_memory(
    access: OperatorAccess,
) -> AsyncIterator[tuple[PersonaProfile, Graphiti]]:
    profile = await load_operator_persona(access.identity, access.settings)
    async with access.open_memory() as memory:
        yield profile, memory
