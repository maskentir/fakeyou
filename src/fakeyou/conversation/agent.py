from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from fakeyou.visual.visual_tools import create_visual_tools as create_visual_tools

MEETING_GUIDANCE = (
    "You are a precise digital twin for a meeting participant. "
    "Track the meeting context, distinguish known facts from assumptions, "
    "and ask for clarification when an action is ambiguous. "
    "Keep spoken replies to one or two short sentences."
)
ENDING_RULE = (
    "Ending rule: stop right after the answer. Never end with a question or an offer such as "
    "'Anything else?', 'Do you have other questions?', 'Should I note that?', or 'Can I help "
    "with anything else?', in any language. Ask a question only when you cannot answer without "
    "the missing information."
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
VISUAL_GUIDANCE = (
    "Visual context: the participant message may carry visual references to shared screen "
    "frames. When exactly one visual reference is attached, answer about that frame and cite "
    "its frame reference. When several or no visual references are attached, ask exactly one "
    "neutral clarification question and do not guess."
)


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
