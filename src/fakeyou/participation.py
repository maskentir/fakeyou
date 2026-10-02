from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    addressed: bool
    followup: bool
    same_participant: bool
    ambiguous: bool
    others_speaking: bool
    activation_valid: bool


@dataclass(frozen=True, slots=True)
class ParticipationDecision:
    action: Literal["reply", "silent", "clarify"]
    reason: str


def choose_participation(context: ParticipationContext) -> ParticipationDecision:
    if context.others_speaking:
        return ParticipationDecision("silent", "floor_occupied")
    if context.addressed:
        if context.ambiguous:
            return ParticipationDecision("clarify", "addressed_ambiguous")
        return ParticipationDecision("reply", "addressed")
    if context.followup and context.activation_valid and context.same_participant:
        if context.ambiguous:
            return ParticipationDecision("clarify", "followup_ambiguous")
        return ParticipationDecision("reply", "followup_same_speaker")
    return ParticipationDecision("silent", "not_addressed")
