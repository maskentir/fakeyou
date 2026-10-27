from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Capability:
    tool: str
    once_per_turn: bool = False
    evidence: bool = False


CAPABILITIES: tuple[Capability, ...] = (
    Capability("recall_meeting_context", once_per_turn=True, evidence=True),
    Capability("observe_meeting", once_per_turn=True, evidence=True),
    Capability("look_at_screen", once_per_turn=True, evidence=True),
    Capability("look_at_participant", once_per_turn=True, evidence=True),
    Capability("look_back_chat", once_per_turn=True),
    Capability("meet_check_action", once_per_turn=True),
)

_BY_TOOL = {capability.tool: capability for capability in CAPABILITIES}

ONCE_PER_TURN_TOOLS = frozenset(
    capability.tool for capability in CAPABILITIES if capability.once_per_turn
)
EVIDENCE_TOOLS = frozenset(capability.tool for capability in CAPABILITIES if capability.evidence)


def capability_for(tool: str) -> Capability | None:
    return _BY_TOOL.get(tool)
