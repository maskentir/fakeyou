import re
from collections.abc import Iterable, Iterator, Mapping
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
_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)


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


def _words(text: str, pattern: re.Pattern[str] = _WORD) -> tuple[str, ...]:
    words = pattern.findall(text.casefold().replace("-", " "))
    return tuple(word for word in words if word != "and")


def _spelled(number: int, language: str) -> tuple[str, ...]:
    return _words(num2words(number, lang=language))


_SCALE_WORDS = frozenset(
    _spelled(10**power, language)[-1]
    for language in SPOKEN_NUMBER_LANGUAGES
    for power in (3, 6, 9, 12)
)
_NUMBER_WORDS = _SCALE_WORDS | frozenset(
    word
    for language in SPOKEN_NUMBER_LANGUAGES
    for number in range(1001)
    for word in _spelled(number, language)
)
_PRONOUN_NUMBERS = frozenset(_spelled(1, language) for language in SPOKEN_NUMBER_LANGUAGES)


def _number_runs(words: tuple[str, ...]) -> Iterator[tuple[str, ...]]:
    run: list[str] = []
    for word in (*words, ""):
        if word in _NUMBER_WORDS:
            run.append(word)
            continue
        while run and tuple(run) not in _PRONOUN_NUMBERS:
            yield tuple(run)
            if run[-1] not in _SCALE_WORDS:
                break
            run.pop()
        run = []


def spoken_numbers(text: str) -> frozenset[tuple[str, ...]]:
    return frozenset(_number_runs(_words(text, _TOKEN)))


def _said_in_words(number: str, spoken: frozenset[tuple[str, ...]]) -> bool:
    return number.isdigit() and any(
        _spelled(int(number), language) in spoken for language in SPOKEN_NUMBER_LANGUAGES
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
    spoken = spoken_numbers(grounded)
    if any(
        number not in written and not _said_in_words(number, spoken)
        for number in numbers_in(sentence)
    ):
        return "invented_number"
    shingles = _shingles(sentence)
    if shingles and any(shingles & _shingles(instruction) for instruction in book.internal):
        return "instruction_disclosure"
    return "pass"
