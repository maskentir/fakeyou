import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fakeyou.foundation.resources import read_package_text
from fakeyou.foundation.transcript import TranscriptEvent

TurnCue = Literal["explicit", "implicit", "none"]

_TURN_CUES: dict[str, Any] = json.loads(read_package_text("turn_cues.json"))


@dataclass(frozen=True, slots=True)
class TurnState:
    spoken_ids: tuple[str, ...] = ()
    floor_holder: str | None = None
    cue: TurnCue = "none"
    cue_target: str | None = None
    my_turn_likely: bool = False


def empty_turn_state() -> TurnState:
    return TurnState()


def _named_cue(folded: str) -> bool:
    prefixes = _TURN_CUES["named_cue_prefixes"]
    markers = _TURN_CUES["named_cue_markers"]
    return folded.startswith(tuple(prefixes)) or any(marker in folded for marker in markers)


def detect_turn_cue(text: str, twin_names: Sequence[str]) -> tuple[TurnCue, str | None]:
    folded = text.strip().casefold()
    openers = "|".join(re.escape(opener) for opener in _TURN_CUES["question_openers"])
    for raw in twin_names:
        name = raw.strip().casefold()
        if not name:
            continue
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", folded) is None:
            continue
        if _named_cue(folded):
            return ("explicit", name)
        if re.search(rf"({openers})\s+{re.escape(name)}", folded) is not None:
            return ("explicit", name)
        if folded.startswith(name):
            return ("explicit", name)
    if folded == "next" or _named_cue(folded):
        return ("implicit", None)
    return ("none", None)


def note_event(state: TurnState, event: TranscriptEvent, twin_names: Sequence[str]) -> TurnState:
    spoken = state.spoken_ids
    if event.participant_id != "unknown" and event.participant_id not in spoken:
        spoken = (*spoken, event.participant_id)
    cue, target = detect_turn_cue(event.text, twin_names)
    aliases = [name.strip().casefold() for name in twin_names if name.strip()]
    mine = cue == "explicit" or (
        cue == "implicit" and not any(alias in spoken for alias in aliases)
    )
    holder = event.participant_id if event.participant_id != "unknown" else state.floor_holder
    return TurnState(
        spoken_ids=spoken,
        floor_holder=holder,
        cue=cue,
        cue_target=target,
        my_turn_likely=mine,
    )
