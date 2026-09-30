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
    "Keep spoken replies to one or two short sentences."
)
ENDING_RULE = (
    "Ending rule: stop right after the answer. Never end with a question or an offer such as "
    "'Ada yang lain?', 'Ada yang mau ditanyakan?', 'Mau aku catat?', or 'Perlu aku bantu?'. "
    "Ask a question only when you cannot answer without the missing information."
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
DEFAULT_PERSONA = f"{MEETING_GUIDANCE} {SPOKEN_LANGUAGE_GUIDANCE['id']}\n\n{ENDING_RULE}"


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
