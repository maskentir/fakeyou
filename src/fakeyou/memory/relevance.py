import json
import re
from collections.abc import Iterable, Sequence
from functools import cache

from graphiti_core.edges import EntityEdge

from fakeyou.foundation.resources import read_package_text

MIN_SHARED_WORDS = 2
MIN_WORD_LENGTH = 3
WORD = re.compile(r"\w+")


@cache
def _function_words() -> frozenset[str]:
    return frozenset(json.loads(read_package_text("assets", "function_words.json")))


def content_words(text: str, *, ignored: Iterable[str] = ()) -> frozenset[str]:
    skipped = _function_words() | {
        word for name in ignored for word in WORD.findall(name.casefold())
    }
    return frozenset(
        word
        for word in WORD.findall(text.casefold())
        if len(word) >= MIN_WORD_LENGTH and word not in skipped
    )


def relevant_edges(
    question: str, edges: Sequence[EntityEdge], *, ignored: Iterable[str] = ()
) -> list[EntityEdge]:
    names = tuple(ignored)
    asked = content_words(question, ignored=names)
    return [
        edge
        for edge in edges
        if len(asked & content_words(edge.fact, ignored=names)) >= MIN_SHARED_WORDS
    ]
