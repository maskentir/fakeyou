import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    addressed: bool
    others_speaking: bool = False


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
