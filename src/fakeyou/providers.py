import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from graphiti_core import Graphiti
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from fakeyou.memory import (
    open_graphiti_memory,
    verify_memory_connection,
)
from fakeyou.provider_faults import classify_exception
from fakeyou.settings import (
    CompatibleReply,
    MemorySettings,
    OpenAIReply,
    ReplySettings,
    Settings,
    require_secret,
)


def preflight_hosts(
    settings: Settings, *, reply: bool = True, memory: bool = False
) -> tuple[str, ...]:
    targets: list[str | None] = [settings.tts.base_url, settings.stt.url]
    if reply:
        targets.append(settings.reply.resolved_base_url)
        if settings.reply_fallback:
            targets.append(settings.reply_fallback.resolved_base_url)
    if memory:
        targets.extend(
            [
                settings.memory.store.uri,
                settings.memory.llm.resolved_base_url,
                settings.memory.embedder.resolved_base_url,
            ]
        )
    return tuple(dict.fromkeys(target for target in targets if target))


def _init_chat_model(role: ReplySettings, name: str) -> BaseChatModel:
    options: dict[str, Any] = {"timeout": role.timeout_seconds, "max_retries": role.max_retries}
    options["base_url"] = role.resolved_base_url
    if isinstance(role, (OpenAIReply, CompatibleReply)) and role.reasoning_effort is not None:
        options["reasoning_effort"] = role.reasoning_effort
    if isinstance(role, CompatibleReply) and role.extra_body is not None:
        options["extra_body"] = role.extra_body
    model: BaseChatModel = init_chat_model(
        role.model,
        model_provider=role.langchain_provider,
        api_key=require_secret(role.api_key, name),
        **options,
    )
    return model


def create_chat_model(settings: Settings) -> BaseChatModel:
    settings.require_roles("reply", "reply_fallback")
    return _init_chat_model(settings.reply, "FAKEYOU_REPLY__API_KEY")


def create_fallback_chat_model(settings: Settings) -> BaseChatModel | None:
    if settings.reply_fallback is None:
        return None
    return _init_chat_model(settings.reply_fallback, "FAKEYOU_REPLY_FALLBACK__API_KEY")


MEMORY_CONNECT_TIMEOUT_SECONDS = 10.0


def memory_config(settings: Settings) -> MemorySettings:
    settings.require_roles("memory.llm", "memory.embedder")
    require_secret(settings.memory.store.password, "FAKEYOU_MEMORY__STORE__PASSWORD")
    return settings.memory


@asynccontextmanager
async def open_persona_memory(settings: Settings) -> AsyncIterator[Graphiti]:
    async with open_graphiti_memory(memory_config(settings)) as memory:
        try:
            async with asyncio.timeout(MEMORY_CONNECT_TIMEOUT_SECONDS):
                await verify_memory_connection(memory)
        except Exception as error:
            raise classify_exception("memory", error) from error
        yield memory
