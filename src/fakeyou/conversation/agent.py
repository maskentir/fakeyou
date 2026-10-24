from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ProviderStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from fakeyou.conversation.middleware import PlainReplyMiddleware, create_reply_retry_middleware
from fakeyou.conversation.reply_planner import ReplyPlan

MEETING_GUIDANCE = (
    "You are a precise digital twin for a meeting participant. "
    "Track the meeting context, distinguish known facts from assumptions, "
    "and ask for clarification when an action is ambiguous. "
    "Keep spoken replies to one or two short sentences. "
    "Participant lines are live speech-recognition transcripts and can contain misheard words: "
    "read each line by its most plausible meaning in this conversation, the way a person who "
    "half-heard a word would, and answer that meaning instead of the literal words."
)
ENDING_RULE = (
    "Ending rule: stop right after the answer. Never end with a question or an offer such as "
    "'Anything else?', 'What else is on your mind?', 'Was that what you were looking for?', "
    "'Should I note that?', or 'Did you want me to look at something?', in any language: do not "
    "check whether your answer helped and do not ask what else the person wants. When someone "
    "asks you to stop or to be shorter, acknowledge it in a few words and stop. Ask a question "
    "only when no plausible reading of the request lets you answer."
)
SPOKEN_LANGUAGE_GUIDANCE = {
    "id": (
        "Reply in the language the participant just used. When their language is unclear, reply "
        "in casual spoken Indonesian like a helpful coworker, using sentences that sound natural "
        "when read aloud."
    ),
    "en": (
        "Reply in the language the participant just used. When their language is unclear, reply "
        "in casual spoken English like a helpful coworker, using sentences that sound natural "
        "when read aloud."
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
        middleware=[*middleware, create_reply_retry_middleware(), PlainReplyMiddleware()],
        checkpointer=checkpointer,
        response_format=ProviderStrategy(ReplyPlan),
    )
