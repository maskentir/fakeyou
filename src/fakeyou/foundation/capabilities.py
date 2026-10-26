from dataclasses import dataclass
from typing import Literal

BriefPermission = Literal["meet_chat", "meet_act"]


@dataclass(frozen=True, slots=True)
class Capability:
    tool: str
    brief_permission: BriefPermission | None = None
    once_per_turn: bool = False
    evidence: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability("recall_meeting_context", once_per_turn=True, evidence=True),
    Capability("observe_meeting", once_per_turn=True, evidence=True),
    Capability("look_at_screen", once_per_turn=True, evidence=True),
    Capability("look_at_participant", once_per_turn=True, evidence=True),
    Capability("look_back_chat", once_per_turn=True),
    Capability("meet_send_chat", brief_permission="meet_chat", once_per_turn=True),
    Capability("meet_raise_hand", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_lower_hand", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_react", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_mute", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_unmute", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_leave", brief_permission="meet_act", once_per_turn=True),
    Capability("meet_check_action"),
)

_BY_TOOL = {capability.tool: capability for capability in CAPABILITIES}

ONCE_PER_TURN_TOOLS = frozenset(
    capability.tool for capability in CAPABILITIES if capability.once_per_turn
)
EVIDENCE_TOOLS = frozenset(capability.tool for capability in CAPABILITIES if capability.evidence)
MEET_TOOLS_BY_PERMISSION: dict[str, frozenset[str]] = {
    permission: frozenset(
        capability.tool for capability in CAPABILITIES if capability.brief_permission == permission
    )
    for permission in ("meet_chat", "meet_act")
}
MEET_ACTION_TOOLS = frozenset(tool for tools in MEET_TOOLS_BY_PERMISSION.values() for tool in tools)


def capability_for(tool: str) -> Capability | None:
    return _BY_TOOL.get(tool)
