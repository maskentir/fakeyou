import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from fakeyou.foundation.transcript import TranscriptEvent

TurnCue = Literal["explicit", "implicit", "none"]


@dataclass(frozen=True, slots=True)
class TurnState:
    spoken_ids: tuple[str, ...] = ()
    floor_holder: str | None = None
    cue: TurnCue = "none"
    cue_target: str | None = None
    my_turn_likely: bool = False


def empty_turn_state() -> TurnState:
    return TurnState()


def detect_turn_cue(text: str, twin_names: Sequence[str]) -> tuple[TurnCue, str | None]:
    folded = text.strip().casefold()
    for raw in twin_names:
        name = raw.strip().casefold()
        if not name:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) is None:
            continue
        if folded.startswith("next") or "you're up" in folded or folded.startswith("silakan"):
            return ("explicit", name)
        if re.search(rf"(gimana|bagaimana)\s+{re.escape(name)}", folded) is not None:
            return ("explicit", name)
        if folded.startswith(name):
            return ("explicit", name)
    if folded == "next" or "you're up" in folded or folded.startswith("silakan"):
        return ("implicit", None)
    return ("none", None)


def note_event(state: TurnState, event: TranscriptEvent, twin_names: Sequence[str]) -> TurnState:
    spoken = state.spoken_ids
    if event.participant_id != "unknown" and event.participant_id not in spoken:
        spoken = (*spoken, event.participant_id)
    cue, target = detect_turn_cue(event.text, twin_names)
    mine = cue == "explicit" or (cue == "implicit" and "harun" not in spoken)
    return TurnState(
        spoken_ids=spoken,
        floor_holder=event.participant_id if event.participant_id != "unknown" else state.floor_holder,
        cue=cue,
        cue_target=target,
        my_turn_likely=mine,
    )
