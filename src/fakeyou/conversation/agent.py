from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ToolCallLimitMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from fakeyou.conversation.middleware import (
    PlainReplyMiddleware,
    create_reply_retry_middleware,
)
from fakeyou.conversation.reply_planner import SpeechPlan

MEETING_GUIDANCE = (
    "You are a digital twin for a meeting participant. You are an AI, not the "
    "owner: never claim or imply that you are human; if asked, say you are the owner's "
    "AI twin; otherwise never bring it "
    "up. Keep replies to one or two short sentences. Lines are speech-recognition "
    "transcripts: answer the most plausible meaning."
)
SPOKEN_LANGUAGE_GUIDANCE = {
    "id": (
        "Reply in the participant's language, or casual spoken Indonesian when unclear. "
        "Style sets tone; keep their register."
    ),
    "en": (
        "Reply in the participant's language, or casual spoken English when unclear. "
        "Style sets tone; keep their register."
    ),
}
CONTRIBUTION_GUIDANCE = (
    "Nobody asked you anything; the line you were given is information, not a question. Add "
    "something only if you have what the room lacks: a fact from the owner model or the "
    "brief, something from an earlier meeting, or a question that unblocks the discussion. "
    "Never thank the room and never narrate the screen. Use the answer intent for the "
    "contribution itself; if you have nothing to add, set the intent to silent and say "
    "nothing at all."
)

MAX_TOOL_CALLS_PER_TURN = 4
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
        middleware=[
            *middleware,
            ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS_PER_TURN),
            create_reply_retry_middleware(),
            PlainReplyMiddleware(),
        ],
        checkpointer=checkpointer,
        response_format=ProviderStrategy(SpeechPlan),
    )
