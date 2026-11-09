from typing import Literal

PACK_VERSION = "core/v1"

AssertionKind = Literal[
    "decision_claim", "proposal", "action_item", "open_question", "owner_decision"
]

EDGE_TYPES: dict[AssertionKind, str] = {
    "decision_claim": "CoreDecisionClaim",
    "proposal": "CoreProposal",
    "action_item": "CoreActionItem",
    "open_question": "CoreOpenQuestion",
    "owner_decision": "CoreOwnerDecision",
}

TYPED_EDGE_TYPES: frozenset[str] = frozenset(EDGE_TYPES.values())

MEETING_LABEL = "CoreMeeting"
ASSERTION_LABEL = "CoreAssertion"


def edge_type_for(kind: AssertionKind) -> str:
    return EDGE_TYPES[kind]
