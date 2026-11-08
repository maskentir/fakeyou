import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from num2words import num2words
from pydantic import BaseModel, ConfigDict, Field

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
    "unparsed_plan",
]
ClaimAct = Literal["fact", "opinion", "proposal", "question", "meta"]
ClaimBasis = Literal[
    "owner_model", "memory", "brief", "meeting_live", "screen", "inference", "none"
]
ClaimCertainty = Literal["stated", "guess"]
ClaimVoice = Literal["owner_proxy", "twin", "reporting"]
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
    times: tuple[datetime, ...] = ()


@dataclass(frozen=True, slots=True)
class FactBook:
    entries: Mapping[str, BookEntry] = field(default_factory=dict)
    heard: str = ""
    internal: tuple[str, ...] = ()

    def with_entries(self, entries: Iterable[BookEntry]) -> "FactBook":
        merged = dict(self.entries)
        merged.update((entry.fact_id, entry) for entry in entries)
        return FactBook(merged, self.heard, self.internal)


class ClaimHeader(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    act: ClaimAct = Field(
        description=(
            "What this claim does. fact: states something that is known. opinion: gives a view. "
            "proposal: suggests a course of action. question: asks the room something. meta: "
            "you about yourself, such as not knowing, not being able to, being the owner's AI "
            "twin, a greeting or an acknowledgement, always with basis none."
        )
    )
    basis: ClaimBasis = Field(
        description=(
            "Where this claim comes from. owner_model: an owner model item. memory: a recalled "
            "memory fact. brief: a meeting brief item. meeting_live: something said or shown in "
            "this meeting. screen: what is on the shared screen. inference: your own conclusion. "
            "none: nothing outside yourself, only for meta."
        )
    )
    refs: tuple[str, ...] = Field(
        description=(
            "The id of every owner model item (om-<id>), recalled memory fact or brief item this "
            "claim states or relies on. Empty when the claim rests on no such source."
        )
    )
    certainty: ClaimCertainty = Field(
        description="stated when the basis says it outright, guess when you are inferring it."
    )
    speaks_as: ClaimVoice = Field(
        description=(
            "Whose voice the claim is in. owner_proxy: the owner's own view, spoken on the "
            "owner's behalf. twin: you as the owner's AI twin. reporting: what a source or "
            "someone else said, attributed to them."
        )
    )


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


def check_plan(
    claims: Sequence[ClaimHeader], *, commits_owner: bool, proactive: bool
) -> SpeechVerdict:
    if commits_owner:
        return "owner_commitment"
    if proactive and not any(claim.refs for claim in claims):
        return "uncited_contribution"
    return "pass"


def check_claim(claim: ClaimHeader, book: FactBook, *, proactive: bool) -> SpeechVerdict:
    for fact_id in claim.refs:
        entry = book.entries.get(fact_id)
        if entry is None:
            return "unknown_citation"
        if entry.disclosure != "room_speakable":
            return "not_speakable"
    if (
        proactive
        and claim.act == "question"
        and not any(book.entries[fact_id].question for fact_id in claim.refs)
    ):
        return "question_not_in_brief"
    return "pass"


def spoken_time_numbers(moment: datetime) -> str:
    return f"{moment.day} {moment.month} {moment.year} {moment.hour} {moment:%H:%M}"


def _grounding(entry: BookEntry) -> str:
    return " ".join([entry.text, *(spoken_time_numbers(moment) for moment in entry.times)])


def check_sentence(sentence: str, refs: Iterable[str], book: FactBook) -> SpeechVerdict:
    grounded = " ".join(
        [
            book.heard,
            *(_grounding(book.entries[fact_id]) for fact_id in refs if fact_id in book.entries),
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
