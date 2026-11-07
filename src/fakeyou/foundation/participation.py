import re
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParticipationContext:
    addressed: bool
    others_speaking: bool = False
    vocative: bool | None = None


def _vocative_patterns(name: str) -> tuple[re.Pattern[str], ...]:
    escaped = re.escape(name)
    return (
        re.compile(rf"^{escaped}(?=$|[\s,.!?;:])"),
        re.compile(rf"^\S+\s+{escaped}(?=$|[,.!?;:])"),
    )


def is_vocative(text: str, aliases: Sequence[str]) -> bool:
    folded = text.strip().casefold()
    for alias in aliases:
        name = alias.strip().casefold()
        if name and any(pattern.search(folded) for pattern in _vocative_patterns(name)):
            return True
    return False


def mentions_whole_phrase(text: str, phrase: str) -> bool:
    name = phrase.strip().casefold()
    if not name:
        return False
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text.casefold()) is not None
