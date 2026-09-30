from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

MEETING_GUIDANCE = (
    "You are a precise digital twin for a meeting participant. "
    "Track the meeting context, distinguish known facts from assumptions, "
    "and ask for clarification when an action is ambiguous. "
    "Keep spoken replies to one or two short sentences. "
    "Answer directly and skip follow-up offers unless you need to confirm something."
)
SPOKEN_LANGUAGE_GUIDANCE = {
    "id": (
        "Reply in casual spoken Indonesian like a helpful coworker, "
        "using sentences that sound natural when read aloud."
    ),
    "en": (
        "Reply in casual spoken English like a helpful coworker, "
        "using sentences that sound natural when read aloud."
    ),
}
DEFAULT_PERSONA = f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE['id']}"


def create_meeting_agent(
    model: BaseChatModel,
    *,
    tools: Sequence[BaseTool] = (),
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    persona: str = DEFAULT_PERSONA,
    middleware: Sequence[AgentMiddleware[Any, Any, Any]] = (),
) -> CompiledStateGraph[Any, Any, Any, Any]:
    configured_tools = list(tools)
    return create_agent(
        model=model,
        tools=configured_tools,
        system_prompt=persona,
        middleware=middleware,
        checkpointer=checkpointer,
    )
