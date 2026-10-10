import re
from contextlib import AsyncExitStack

from langchain_core.tools import BaseTool

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_text
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.conversation.agent import (
    create_meeting_agent,
)
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, require_url
from fakeyou.foundation.invariants import IDENTIFIER_PATTERN
from fakeyou.meet.browser import BrowserLaunch, open_browser_tools
from fakeyou.memory.memory import (
    create_memory_tools,
    open_graphiti_memory,
)
from fakeyou.orchestration.assembly import default_persona
from fakeyou.orchestration.providers import create_chat_model, memory_config


async def chat(
    message: str, thread: str, *, use_browser: bool, use_memory: bool, settings: Settings
) -> None:
    if not re.fullmatch(IDENTIFIER_PATTERN, thread):
        raise FakeYouError(ErrorCode.CHAT_THREAD_INVALID)
    model = create_chat_model(settings)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(require_url(configured_url(settings)))
        )
        tools: list[BaseTool] = []
        if use_memory:
            memory = await resources.enter_async_context(
                open_graphiti_memory(memory_config(settings))
            )
            tools.extend(
                create_memory_tools(
                    memory, group_id=settings.memory.store.group, recall=settings.memory.recall
                )
            )
        if use_browser:
            tools.extend(
                await resources.enter_async_context(
                    open_browser_tools(
                        launch=BrowserLaunch(
                            headless=settings.browser.headless,
                            license_key=settings.browser.license_key.get_secret_value()
                            if settings.browser.license_key
                            else None,
                        )
                    )
                )
            )
        agent = create_meeting_agent(
            model, tools=tools, checkpointer=checkpointer, persona=default_persona(settings)
        )
        remember = next((item for item in tools if item.name == "remember_meeting_fact"), None)
        try:
            response = await agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config={"configurable": {"thread_id": thread}, "recursion_limit": 30},
            )
        finally:
            if remember is not None:
                await remember.ainvoke({"fact": "", "flush": True})
        emit_text(response["messages"][-1].content)
