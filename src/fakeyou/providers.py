import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from graphiti_core import Graphiti
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory import (
    open_graphiti_memory,
    verify_memory_connection,
)
from fakeyou.provider_faults import classify_exception
from fakeyou.settings import (
    AnthropicReply,
    CompatibleReply,
    GoogleReply,
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


def _init_chat_model(role: ReplySettings, name: ConfigKey) -> BaseChatModel:
    options: dict[str, Any] = {"timeout": role.timeout_seconds, "max_retries": role.max_retries}
    options["base_url"] = role.resolved_base_url
    if (
        isinstance(role, (OpenAIReply, CompatibleReply, GoogleReply))
        and role.reasoning_effort is not None
    ):
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
    return _init_chat_model(settings.reply, ConfigKey.REPLY_API_KEY)


def create_debrief_chat_model(settings: Settings) -> BaseChatModel:
    if settings.debrief is None:
        return create_chat_model(settings)
    settings.require_roles("debrief", "reply_fallback")
    return _init_chat_model(settings.debrief, ConfigKey.DEBRIEF_API_KEY)


def create_fallback_chat_model(settings: Settings) -> BaseChatModel | None:
    if settings.reply_fallback is None:
        return None
    return _init_chat_model(settings.reply_fallback, ConfigKey.REPLY_FALLBACK_API_KEY)


def create_visual_chat_model(settings: Settings) -> BaseChatModel:
    settings.require_roles("reply")
    visual = settings.visual
    reply = settings.reply
    values: dict[str, Any] = {
        "model": visual.model,
        "api_key": reply.api_key,
        "timeout_seconds": visual.timeout_seconds,
        "max_retries": reply.max_retries,
    }
    if visual.base_url is not None:
        values["base_url"] = visual.base_url
    provider = visual.provider
    if provider == "openai":
        role: ReplySettings = OpenAIReply(**values)
    elif provider == "anthropic":
        role = AnthropicReply(**values)
    elif provider == "google":
        role = GoogleReply(**values)
    elif provider == "openai_compatible":
        if visual.base_url is None:
            raise FakeYouError(ErrorCode.CONFIG_VISUAL_BASE_URL_REQUIRED)
        role = CompatibleReply(**values)
    else:
        raise FakeYouError(ErrorCode.VISUAL_PROVIDER_UNSUPPORTED, provider=provider)
    return _init_chat_model(role, ConfigKey.REPLY_API_KEY)


MEMORY_CONNECT_TIMEOUT_SECONDS = 10.0


def memory_config(settings: Settings) -> MemorySettings:
    settings.require_roles("memory.llm", "memory.embedder")
    require_secret(settings.memory.store.password, ConfigKey.MEMORY_STORE_PASSWORD)
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
