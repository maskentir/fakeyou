import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from num2words import num2words
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.session.facts import Confidence, Disclosure, Namespace, OwnerModelKind

SpeechVerdict = Literal[
    "pass",
    "unknown_citation",
    "not_speakable",
    "invented_number",
    "instruction_disclosure",
    "owner_commitment",
    "unparsed_plan",
    "basis_mismatch",
    "unbased_claim",
    "meta_with_content",
    "opinion_without_position",
    "question_without_basis",
    "guess_as_fact",
    "speaks_as_mismatch",
    "critic_refused",
]
ClaimAct = Literal["fact", "opinion", "proposal", "question", "meta"]
ClaimBasis = Literal[
    "owner_model", "memory", "brief", "meeting_live", "screen", "inference", "none"
]
ClaimCertainty = Literal["stated", "guess"]
ClaimVoice = Literal["owner_proxy", "twin", "reporting"]
EntryKind = OwnerModelKind | Literal["question", "objective"]
CITED_NAMESPACES: Mapping[ClaimBasis, Namespace] = {
    "owner_model": "owner_model",
    "memory": "memory",
    "brief": "brief",
    "screen": "screen",
}
SOURCED_BASES: frozenset[ClaimBasis] = frozenset({"owner_model", "memory", "brief", "screen"})
OWNER_STANCES: frozenset[EntryKind] = frozenset({"position", "preference", "decision_rule"})
QUESTION_GROUNDS: frozenset[EntryKind] = frozenset(
    {"question", "objective", "position", "decision_rule"}
)
OWNER_VIEWS: frozenset[ClaimAct] = frozenset({"opinion", "proposal"})
VOICES: Mapping[ClaimBasis, frozenset[ClaimVoice]] = {
    "owner_model": frozenset({"owner_proxy", "reporting", "twin"}),
    "brief": frozenset({"owner_proxy", "reporting", "twin"}),
    "memory": frozenset({"reporting"}),
    "screen": frozenset({"reporting", "twin"}),
    "meeting_live": frozenset({"reporting", "twin"}),
    "inference": frozenset({"twin"}),
    "none": frozenset({"twin"}),
}
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
    kind: EntryKind = "fact"
    confidence: Confidence = "firm"
    owner_spoken: bool = False
    times: tuple[datetime, ...] = ()
    attributed_to: str | None = None


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
            "What this claim does. fact: states something known. opinion: gives the owner's "
            "view, and proposal: suggests a course of action, both only from an owner model "
            "position, preference or decision rule. question: asks the room, only from a brief "
            "question, a brief objective or an owner position or decision rule. meta: only you "
            "about yourself, such as not knowing, not being able to, being the owner's AI twin, "
            "a greeting, an acknowledgement or asking someone to repeat or clarify; a meta claim "
            "has no refs and no numbers."
        )
    )
    basis: ClaimBasis = Field(
        description=(
            "Where this claim comes from. owner_model, memory, brief: the owner model items, "
            "recalled memory facts or brief items in refs, and only that kind. meeting_live: "
            "something said in this meeting. screen: what is on the shared screen, with that "
            "screen in refs. inference: your own conclusion. none: nothing outside yourself, "
            "only for meta."
        )
    )
    refs: tuple[str, ...] = Field(
        description=(
            "The id of every owner model item (om-<id>), recalled memory fact, brief item or "
            "shared screen (screen#<n>, as named in the meeting view) this claim states or "
            "relies on, all of the kind its basis names. Empty for any other basis."
        )
    )
    certainty: ClaimCertainty = Field(
        description=(
            "stated when the basis says it outright. guess for an inference and for any owner "
            "model item whose confidence is leaning, said as where the owner is leaning."
        )
    )
    speaks_as: ClaimVoice = Field(
        description=(
            "Whose voice the claim is in. owner_proxy: the owner's own view on the owner's "
            "behalf, from owner_model or brief. reporting: what a source or someone else said, "
            "attributed to them; memory is always reporting unless the owner said it. twin: you "
            "as the owner's AI twin, stating a fact or asking, and always for inference and "
            "none. An opinion or a proposal is the owner's, never the twin's own."
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
    if proactive and not any(claim.basis in SOURCED_BASES for claim in claims):
        return "unbased_claim"
    return "pass"


def _cited(claim: ClaimHeader, book: FactBook) -> tuple[BookEntry, ...] | SpeechVerdict:
    entries = tuple(book.entries.get(fact_id) for fact_id in claim.refs)
    if None in entries:
        return "unknown_citation"
    cited = tuple(entry for entry in entries if entry is not None)
    if any(not _speakable(entry, claim) for entry in cited):
        return "not_speakable"
    return cited


def _speakable(entry: BookEntry, claim: ClaimHeader) -> bool:
    if entry.disclosure == "internal_instruction":
        return claim.act == "question"
    return entry.disclosure == "room_speakable"


def _based(claim: ClaimHeader, cited: tuple[BookEntry, ...]) -> bool:
    namespace = CITED_NAMESPACES.get(claim.basis)
    if namespace is None:
        return not cited
    return bool(cited) and all(entry.namespace == namespace for entry in cited)


def _voiced(claim: ClaimHeader, cited: tuple[BookEntry, ...]) -> bool:
    if claim.speaks_as == "twin" and claim.act in OWNER_VIEWS:
        return False
    if claim.speaks_as in VOICES[claim.basis]:
        return True
    return (
        claim.basis == "memory"
        and claim.speaks_as == "owner_proxy"
        and all(entry.owner_spoken for entry in cited)
    )


def check_claim(claim: ClaimHeader, book: FactBook) -> SpeechVerdict:
    if claim.act == "meta" and claim.refs:
        return "meta_with_content"
    cited = _cited(claim, book)
    if isinstance(cited, str):
        return cited
    kinds = {entry.kind for entry in cited}
    if not _based(claim, cited):
        return "basis_mismatch"
    if claim.act != "meta" and claim.basis == "none":
        return "unbased_claim"
    if claim.act in ("opinion", "proposal") and not kinds & OWNER_STANCES:
        return "opinion_without_position"
    if claim.act == "question" and not kinds & QUESTION_GROUNDS:
        return "question_without_basis"
    if claim.certainty == "stated" and (
        claim.basis == "inference" or any(entry.confidence == "leaning" for entry in cited)
    ):
        return "guess_as_fact"
    if not _voiced(claim, cited):
        return "speaks_as_mismatch"
    return "pass"


def spoken_time_numbers(moment: datetime) -> str:
    return f"{moment.day} {moment.month} {moment.year} {moment.hour} {moment:%H:%M}"


def _grounding(entry: BookEntry) -> str:
    return " ".join([entry.text, *(spoken_time_numbers(moment) for moment in entry.times)])


def check_sentence(sentence: str, claim: ClaimHeader | None, book: FactBook) -> SpeechVerdict:
    if claim is not None and claim.act == "meta":
        return "meta_with_content" if _counts(sentence) else _undisclosed(sentence, book)
    refs = claim.refs if claim is not None else ()
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
    return _undisclosed(sentence, book)


def _counts(sentence: str) -> bool:
    return bool(numbers_in(sentence) or spoken_numbers(sentence))


def _undisclosed(sentence: str, book: FactBook) -> SpeechVerdict:
    shingles = _shingles(sentence)
    if shingles and any(shingles & _shingles(instruction) for instruction in book.internal):
        return "instruction_disclosure"
    return "pass"
