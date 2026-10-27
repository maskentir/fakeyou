import json
from collections.abc import Callable

from langchain_core.tools import BaseTool, tool

from fakeyou.session.commands import CommandOutcome

MEET_CHECK_ACTION = "meet_check_action"


def create_action_status_tool(recent: Callable[[], tuple[CommandOutcome, ...]]) -> BaseTool:
    @tool(
        MEET_CHECK_ACTION,
        description=(
            "List your most recent meeting actions and chat sends with their verified state: "
            "effect_observed means the meeting confirmed it, effect_unobserved means it was "
            "clicked without confirmation, unknown means it may or may not have happened, "
            "anything else means it did not happen."
        ),
    )
    async def meet_check_action() -> str:
        return json.dumps(
            [{"action": outcome.kind, "state": outcome.state} for outcome in recent()],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return meet_check_action
