import json
import re
from functools import cache

from fakeyou.foundation.resources import read_package_text

_TABLES: dict[str, dict[str, object]] = json.loads(read_package_text("number_vocabulary.json"))
_WORD = re.compile(r"[a-z]+")
MAX_PHRASE_WORDS = 3
SMALL_WORD_LIMIT = 10
TENS = frozenset(range(20, 100, 10))
UNITS = frozenset(range(0, 10))


@cache
def phrases() -> dict[str, int]:
    merged: dict[str, int] = {}
    for table in _TABLES.values():
        phrases_of = table["phrases"]
        assert isinstance(phrases_of, dict)
        merged.update(phrases_of)
    return merged


def keyword_table(language: str) -> tuple[str, ...]:
    table = _TABLES.get(language.split("-", 1)[0].casefold())
    if table is None:
        return ()
    keywords = table["keywords"]
    assert isinstance(keywords, list)
    return tuple(str(word) for word in keywords)


def _word_numbers(text: str) -> set[int]:
    table = phrases()
    tokens = _WORD.findall(text.casefold())
    found: set[int] = set()
    index = 0
    while index < len(tokens):
        matched = _match(table, tokens, index)
        if matched is None:
            index += 1
            continue
        value, consumed = matched
        found.add(value)
        index += consumed
        if value in TENS and index < len(tokens):
            unit = table.get(tokens[index])
            if unit in UNITS:
                found.add(value + unit)
                index += 1
    return found


def _match(table: dict[str, int], tokens: list[str], index: int) -> tuple[int, int] | None:
    for size in range(MAX_PHRASE_WORDS, 0, -1):
        if index + size > len(tokens):
            continue
        phrase = " ".join(tokens[index : index + size])
        value = table.get(phrase)
        if value is not None:
            return value, size
    return None


def digits_in(text: str) -> frozenset[str]:
    return frozenset(
        match.group().rstrip(".,:/") for match in re.finditer(r"\d+(?:[.,:/]\d+)*", text)
    )


def numbers_in(text: str) -> frozenset[str]:
    return digits_in(text) | {str(value) for value in _word_numbers(text)}


def claimed_numbers(text: str) -> frozenset[str]:
    words = {str(value) for value in _word_numbers(text) if value >= SMALL_WORD_LIMIT}
    return digits_in(text) | words
