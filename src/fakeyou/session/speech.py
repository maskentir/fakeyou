import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from num2words import num2words

from fakeyou.session.facts import Disclosure, Namespace

SpeechVerdict = Literal[
    "pass",
    "unknown_citation",
    "not_speakable",
    "invented_number",
    "instruction_disclosure",
    "owner_commitment",
    "uncited_contribution",
    "question_not_in_brief",
]
ContributionKind = Literal["none", "fact", "correction", "question"]
DISCLOSURE_SHINGLE_WORDS = 6
SPOKEN_NUMBER_LANGUAGES = ("id", "en")
_NUMBER = re.compile(r"\d+(?:[.,:/]\d+)*")
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class BookEntry:
    fact_id: str
    text: str
    namespace: Namespace
    disclosure: Disclosure = "room_speakable"
    question: bool = False


@dataclass(frozen=True, slots=True)
class FactBook:
    entries: Mapping[str, BookEntry] = field(default_factory=dict)
    heard: str = ""
    internal: tuple[str, ...] = ()

    def with_entries(self, entries: Iterable[BookEntry]) -> "FactBook":
        merged = dict(self.entries)
        merged.update((entry.fact_id, entry) for entry in entries)
        return FactBook(merged, self.heard, self.internal)


@dataclass(frozen=True, slots=True)
class SpeechAct:
    cites: tuple[str, ...] = ()
    commits_owner: bool = False
    contribution: ContributionKind = "none"


def numbers_in(text: str) -> frozenset[str]:
    return frozenset(match.group().rstrip(".,:/") for match in _NUMBER.finditer(text))


def _words(text: str) -> str:
    words = _WORD.findall(text.casefold().replace("-", " "))
    return f" {' '.join(word for word in words if word != 'and')} "


def _said_in_words(number: str, spoken: str) -> bool:
    if not number.isdigit():
        return False
    return any(
        _words(num2words(int(number), lang=language)) in spoken
        for language in SPOKEN_NUMBER_LANGUAGES
    )


def _shingles(text: str) -> frozenset[tuple[str, ...]]:
    words = [word.casefold() for word in _WORD.findall(text)]
    size = DISCLOSURE_SHINGLE_WORDS
    return frozenset(tuple(words[index : index + size]) for index in range(len(words) - size + 1))


def check_act(act: SpeechAct, book: FactBook) -> SpeechVerdict:
    if act.commits_owner:
        return "owner_commitment"
    for fact_id in act.cites:
        entry = book.entries.get(fact_id)
        if entry is None:
            return "unknown_citation"
        if entry.disclosure != "room_speakable":
            return "not_speakable"
    if act.contribution == "none":
        return "pass"
    if not act.cites:
        return "uncited_contribution"
    if act.contribution == "question" and not any(
        book.entries[fact_id].question for fact_id in act.cites
    ):
        return "question_not_in_brief"
    return "pass"


def check_sentence(sentence: str, act: SpeechAct, book: FactBook) -> SpeechVerdict:
    grounded = " ".join(
        [
            book.heard,
            *(book.entries[fact_id].text for fact_id in act.cites if fact_id in book.entries),
        ]
    )
    written = numbers_in(grounded)
    spoken = _words(grounded)
    if any(
        number not in written and not _said_in_words(number, spoken)
        for number in numbers_in(sentence)
    ):
        return "invented_number"
    shingles = _shingles(sentence)
    if shingles and any(shingles & _shingles(instruction) for instruction in book.internal):
        return "instruction_disclosure"
    return "pass"
