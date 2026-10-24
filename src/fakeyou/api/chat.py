from contextlib import AsyncExitStack
from functools import partial
from typing import cast

from langchain_core.tools import BaseTool

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import verify_chat_thread_id
from fakeyou.config.settings import Settings
from fakeyou.conversation.agent import create_meeting_agent
from fakeyou.conversation.middleware import HistoryTrimMiddleware, OrphanToolCallMiddleware
from fakeyou.conversation.reply_planner import ReplyPlan
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, require_url
from fakeyou.meet.browser import BrowserLaunch, open_browser_tools
from fakeyou.memory.memory import create_memory_tools, open_graphiti_memory
from fakeyou.memory.persona import memory_group_for
from fakeyou.observability.faults import capture_error
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.orchestration.assembly import default_persona
from fakeyou.orchestration.providers import create_chat_model, memory_config


async def chat(
    message: str, thread: str, *, use_browser: bool, use_memory: bool, settings: Settings
) -> str:
    verify_chat_thread_id(thread)
    reply, error = await capture_error(
        partial(
            _chat_turn,
            message,
            thread,
            use_browser=use_browser,
            use_memory=use_memory,
            settings=settings,
        )
    )
    if error is None:
        return cast(str, reply)
    if isinstance(error, (FakeYouError, ProviderFault)):
        raise error
    raise FakeYouError(ErrorCode.CHAT_FAILED, reason=type(error).__name__) from error


async def _chat_turn(
    message: str, thread: str, *, use_browser: bool, use_memory: bool, settings: Settings
) -> str:
    model = create_chat_model(settings)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(
                require_url(configured_url(settings)),
                connect_timeout_seconds=settings.database.connect_timeout_seconds,
                statement_timeout_seconds=settings.database.statement_timeout_seconds,
                keepalive_idle_seconds=settings.database.keepalive_idle_seconds,
            )
        )
        tools: list[BaseTool] = []
        if use_memory:
            memory = await resources.enter_async_context(
                open_graphiti_memory(memory_config(settings))
            )
            tools.extend(
                create_memory_tools(
                    memory,
                    group_id=memory_group_for(
                        settings.identity.owner_id, settings.identity.twin_id
                    ),
                    recall=settings.memory.recall,
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
                        ),
                        output_dir=settings.paths.browser_output_dir,
                    )
                )
            )
        agent = create_meeting_agent(
            model,
            tools=tools,
            checkpointer=checkpointer,
            persona=default_persona(settings),
            middleware=[
                HistoryTrimMiddleware(max_tokens=settings.conversation.history_max_tokens),
                OrphanToolCallMiddleware(),
            ],
        )
        response = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread}, "recursion_limit": 30},
        )
        plan = response.get("structured_response")
        if isinstance(plan, ReplyPlan):
            return plan.text
        return str(response["messages"][-1].content)
