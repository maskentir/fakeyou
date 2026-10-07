import asyncio
import signal
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager, contextmanager
from typing import Any

from graphiti_core import Graphiti
from langchain.agents.middleware import AgentMiddleware, ModelFallbackMiddleware
from langchain_core.tools import BaseTool

from fakeyou.agent import (
    ENDING_RULE,
    MEETING_GUIDANCE,
    SPOKEN_LANGUAGE_GUIDANCE,
    create_meeting_agent,
)
from fakeyou.brief import MeetingBrief
from fakeyou.checkpoint import checkpoint_dsn, open_checkpointer
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    ConversationResponder,
    LangGraphResponder,
    StreamingLangGraphResponder,
    ToolResultCallback,
    conversation_thread_id,
)
from fakeyou.faults import FaultReporter
from fakeyou.greetings import create_greeting_middleware
from fakeyou.journal import Journal
from fakeyou.memory import (
    create_memory_prefetch_middleware,
    create_memory_tools,
    create_recall_error_middleware,
)
from fakeyou.persona import (
    PersonaProfile,
    PersonaTool,
    build_system_prompt,
    meeting_tool_names,
    memory_wiring,
)
from fakeyou.providers import create_chat_model, create_fallback_chat_model
from fakeyou.session import SessionConfig
from fakeyou.settings import (
    Settings,
)


def default_persona(settings: Settings) -> str:
    return (
        f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE[settings.speech.language]}\n\n{ENDING_RULE}"
    )


@contextmanager
def stop_signal_handlers(stop: asyncio.Event) -> Iterator[None]:
    loop = asyncio.get_running_loop()
    with ExitStack() as handlers:
        for candidate in (signal.SIGINT, signal.SIGTERM):
            previous = signal.getsignal(candidate)
            try:
                loop.add_signal_handler(candidate, stop.set)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            handlers.callback(signal.signal, candidate, previous)
            handlers.callback(loop.remove_signal_handler, candidate)
        yield


@asynccontextmanager
async def open_conversation(
    settings: Settings,
    config: SessionConfig,
    *,
    graphiti: Graphiti | None = None,
    profile: PersonaProfile | None = None,
    on_transition: Callable[[ConversationPhase, ConversationPhase, str], None],
    wake_phrases: Sequence[str] = (),
    barge_in: bool = False,
    faults: FaultReporter | None = None,
    journal: Journal | None = None,
    on_tool_result: ToolResultCallback | None = None,
    brief: MeetingBrief | None = None,
) -> AsyncIterator[ConversationLoop]:
    tool_names: frozenset[PersonaTool] = frozenset()
    if profile is None:
        if brief is not None:
            raise ValueError("a meeting brief requires a persona profile")
        persona = default_persona(settings)
    else:
        tool_names = meeting_tool_names(profile, brief, memory_requested=graphiti is not None)
        persona = build_system_prompt(
            profile.model_copy(update={"allowed_tools": tool_names}),
            brief=brief,
            recall_strategy=settings.memory.recall.strategy,
        )
    model = create_chat_model(settings)
    async with AsyncExitStack() as resources:
        checkpointer = await resources.enter_async_context(
            open_checkpointer(settings.paths.checkpoint_path, dsn=checkpoint_dsn(settings))
        )
        tools: list[BaseTool] = []
        middleware: list[AgentMiddleware[Any, Any, Any]] = []
        greeting_language = profile.language if profile is not None else settings.speech.language
        middleware.append(create_greeting_middleware(greeting_language))
        fallback = create_fallback_chat_model(settings)
        if fallback is not None:
            middleware.append(ModelFallbackMiddleware(fallback))
        if graphiti is not None and profile is not None and tool_names:
            bound, prefetch = memory_wiring(tool_names, settings.memory.recall.strategy)
            if prefetch:
                middleware.append(
                    create_memory_prefetch_middleware(
                        graphiti,
                        group_id=profile.memory_group,
                        recall=settings.memory.recall,
                        faults=faults,
                        journal=journal,
                    )
                )
            if bound:
                tools.extend(
                    tool
                    for tool in create_memory_tools(
                        graphiti, group_id=profile.memory_group, recall=settings.memory.recall
                    )
                    if tool.name in bound
                )
                middleware.append(create_recall_error_middleware(faults))
        agent = create_meeting_agent(
            model,
            tools=tools,
            checkpointer=checkpointer,
            persona=persona,
            middleware=middleware,
        )
        thread_id = conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id)
        responder: ConversationResponder
        if settings.conversation.sentence_streaming:
            responder = StreamingLangGraphResponder(
                agent,
                thread_id,
                journal=journal,
                on_tool_result=on_tool_result,
                language=settings.speech.language,
                max_sentences=settings.conversation.max_spoken_sentences,
            )
        else:
            responder = LangGraphResponder(
                agent,
                thread_id,
                journal=journal,
                on_tool_result=on_tool_result,
            )
        conversation = ConversationLoop(
            responder,
            on_transition=on_transition,
            wake_phrases=wake_phrases,
            barge_in=barge_in,
            response_timeout_seconds=settings.conversation.response_timeout_seconds,
            activation_seconds=settings.conversation.activation_seconds,
            barge_in_min_words=settings.conversation.barge_in_min_words,
            max_spoken_sentences=settings.conversation.max_spoken_sentences,
            language=settings.speech.language,
            sentence_streaming=settings.conversation.sentence_streaming,
            faults=faults,
            journal=journal,
            participant_policy_enabled=settings.conversation.participant_policy_enabled,
            pre_generate_enabled=settings.conversation.pre_generate_enabled,
            pregen_fuzzy_restart_enabled=settings.conversation.pregen_fuzzy_restart_enabled,
        )
        resources.push_async_callback(conversation.close)
        yield conversation
