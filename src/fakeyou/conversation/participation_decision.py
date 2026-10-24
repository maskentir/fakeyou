from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ParticipationAction = Literal["reply", "silent", "clarify"]

_PARTICIPATION_ACTIONS: dict[str, ParticipationAction] = {
    "speak": "reply",
    "silent": "silent",
    "clarify": "clarify",
}


class ParticipationDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Literal["speak", "silent", "clarify"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1)
    backchannel: bool = False


def to_participation_action(decision: ParticipationDecision) -> ParticipationAction:
    return _PARTICIPATION_ACTIONS[decision.decision]
