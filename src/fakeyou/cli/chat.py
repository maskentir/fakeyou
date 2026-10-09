import re
from contextlib import AsyncExitStack

from langchain_core.tools import BaseTool

from fakeyou.agent import (
    create_meeting_agent,
)
from fakeyou.assembly import default_persona
from fakeyou.browser import open_browser_tools
from fakeyou.checkpoint import checkpoint_dsn, open_checkpointer
from fakeyou.cli.errors import CliError
from fakeyou.memory import (
    create_memory_tools,
    open_graphiti_memory,
)
from fakeyou.providers import create_chat_model, memory_config
from fakeyou.settings import (
    Settings,
)


async def chat(
    message: str, thread: str, use_browser: bool, use_memory: bool, settings: Settings
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread):
        raise CliError("--thread must be 1-128 characters using letters, numbers, '_' or '-'")
    model = create_chat_model(settings)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(settings.paths.checkpoint_path, dsn=checkpoint_dsn(settings))
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
                        headless=settings.browser.headless,
                        license_key=settings.browser.license_key.get_secret_value()
                        if settings.browser.license_key
                        else None,
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
        print(response["messages"][-1].content)
