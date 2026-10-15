import re
from collections.abc import Sequence
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
    bare_ack: bool = False
    interjection: bool = False
    awaiting_reply: bool = False
    turn_cue: str = "none"
    turn_mine: bool = False


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
    if not (context.followup and context.activation_valid and context.same_participant):
        return ParticipationDecision("silent", "not_addressed")
    if context.interjection:
        return ParticipationDecision("silent", "interjection")
    if context.bare_ack and not context.awaiting_reply:
        return ParticipationDecision("silent", "bare_ack")
    if context.ambiguous:
        return ParticipationDecision("clarify", "followup_ambiguous")
    if context.bare_ack:
        return ParticipationDecision("reply", "followup_answers_question")
    return ParticipationDecision("reply", "followup_same_speaker")


_VOCATIVE_BOUNDARY = frozenset({" ", ",", ".", "!", "?", ";", ":"})


def is_vocative(text: str, aliases: Sequence[str]) -> bool:
    folded = text.strip().casefold()
    for alias in aliases:
        name = alias.strip().casefold()
        if not name or not folded.startswith(name):
            continue
        rest = folded[len(name) :]
        if not rest or rest[0] in _VOCATIVE_BOUNDARY:
            return True
    return False


def mentions_whole_phrase(text: str, phrase: str) -> bool:
    name = phrase.strip().casefold()
    if not name:
        return False
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text.casefold()) is not None


def same_observed_speaker(previous_id: str | None, current_id: str) -> bool:
    if previous_id is None or previous_id == "unknown" or current_id == "unknown":
        return False
    return previous_id == current_id


def same_voice(engaged: str | None, clusters: Sequence[str]) -> bool | None:
    if engaged is None or not clusters:
        return None
    return tuple(clusters) == (engaged,)


def is_other_observed_speaker(previous_id: str | None, current_id: str) -> bool:
    if previous_id is None or previous_id == "unknown" or current_id == "unknown":
        return False
    return previous_id != current_id
