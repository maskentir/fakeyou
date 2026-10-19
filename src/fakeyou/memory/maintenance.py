from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from graphiti_core import Graphiti

from fakeyou.memory.memory import ReembedResult, ensure_indexes, reembed_group
from fakeyou.memory.operator_access import OperatorAccess, open_operator_memory


async def initialize_indexes(
    open_memory: Callable[[], AbstractAsyncContextManager[Graphiti]],
) -> None:
    async with open_memory() as memory:
        await ensure_indexes(memory)


async def run_reembed(access: OperatorAccess) -> ReembedResult:
    async with open_operator_memory(access) as (profile, memory):
        return await reembed_group(memory, group_id=profile.memory_group)
