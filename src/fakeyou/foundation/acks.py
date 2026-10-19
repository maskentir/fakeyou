import json
from typing import Any

from fakeyou.foundation.resources import read_package_text

ACK_VOCABULARY: dict[str, Any] = json.loads(read_package_text("ack_vocabulary.json"))
BARE_ACK_VOCABULARY: frozenset[str] = frozenset(ACK_VOCABULARY["bare_acks"])
INTERJECTION_OPENERS: tuple[tuple[str, ...], ...] = tuple(
    tuple(opener.split()) for opener in ACK_VOCABULARY["interjections"]
)
_ACK_EDGE_PUNCTUATION = '.,!;:"“”‘’()[]{}-–—…'


def _ack_words(text: str) -> list[str]:
    words = [word.strip(_ACK_EDGE_PUNCTUATION) for word in text.casefold().split()]
    return [word for word in words if word]


def is_bare_ack(text: str) -> bool:
    if "?" in text:
        return False
    words = _ack_words(text)
    return len(words) == 1 and words[0] in BARE_ACK_VOCABULARY


def is_interjection(text: str) -> bool:
    if "?" in text:
        return False
    words = _ack_words(text)
    return any(tuple(words[: len(opener)]) == opener for opener in INTERJECTION_OPENERS)
