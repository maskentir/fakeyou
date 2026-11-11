from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import groupby
from typing import Literal, NamedTuple, TypedDict, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.state_speech import locale_of, speech_line
from fakeyou.foundation.sentences import split_sentences
from fakeyou.session.speech import (
    ClaimAct,
    ClaimBasis,
    ClaimCertainty,
    ClaimHeader,
    ClaimVoice,
    FactBook,
    SpeechVerdict,
    check_claim,
    check_plan,
    check_sentence,
)

ReplyIntent = Literal[
    "answer",
    "ack",
    "clarify",
    "decline",
    "silent",
    "stop",
    "shorter",
    "repeat",
    "louder",
]
ReplyLanguage = Literal["id", "en"]

REPLY_LANGUAGES: frozenset[str] = frozenset(get_args(ReplyLanguage))
SHORT_INTENTS = frozenset({"ack", "decline", "stop", "shorter", "louder"})
JSON_START = "{"
GateOutcome = Literal["pass", "drop", "n/a"]

IntentHandler = Callable[[ReplyIntent], str | None]


class Claim(ClaimHeader):
    say: str = Field(
        description=(
            "The words you speak aloud for this claim and nothing else, every word in the "
            "language you declared: English when language is en, Indonesian when it is id, even "
            "when your profile and the meeting use the other one."
        )
    )


class SpeechPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    understood: str = Field(
        description=(
            "The participant's last line as you understood it, in the language it was spoken, "
            "with misheard words corrected to their most plausible meaning."
        )
    )
    language: ReplyLanguage = Field(
        description=(
            "The language of understood: en for English words, id for Indonesian words. The "
            "participant's words decide it, never the language of your profile, your speaking "
            "style or the rest of the meeting."
        )
    )
    intent: ReplyIntent = Field(
        description=(
            "What this reply does. answer: answers a question. ack: a short acknowledgement. "
            "clarify: only when no reading of the request makes sense. decline: you must not "
            "agree, approve or decide. silent: you should not speak at all. stop, shorter, repeat, "
            "louder: the participant asked you to stop talking, to be more concise, to say that "
            "again, or to speak louder; the claims then say only a few words acknowledging it."
        )
    )
    commits_owner: bool = Field(
        default=False,
        description=(
            "True when the reply would agree, approve, promise, accept a deadline or price, or "
            "decide anything on the owner's behalf."
        ),
    )
    claims: list[Claim] = Field(
        description=(
            "What you say, as claims in speaking order, one or two short sentences in all. Each "
            "claim rests on one basis and its say holds the words for that claim alone; a "
            "sentence that draws on two sources becomes two claims. A contribution nobody asked "
            "for needs at least one claim whose basis is owner_model, memory, brief or screen. "
            "Empty when the intent is silent."
        )
    )

    @property
    def text(self) -> str:
        return " ".join(claim.say for claim in self.claims)


class _ClaimDraft(TypedDict, total=False):
    act: ClaimAct
    basis: ClaimBasis
    refs: tuple[str, ...]
    certainty: ClaimCertainty
    speaks_as: ClaimVoice
    say: str


class _PlanDraft(TypedDict, total=False):
    understood: str
    language: ReplyLanguage
    intent: ReplyIntent
    commits_owner: bool
    claims: list[_ClaimDraft]


PLAN_DRAFT = TypeAdapter(_PlanDraft)


class SpeechPiece(NamedTuple):
    claim: int
    text: str


@dataclass(slots=True)
class SpeechGate:
    compose: Callable[[], FactBook]
    proactive: bool = False
    verify: bool = True
    _book: FactBook | None = None

    @property
    def book(self) -> FactBook:
        if self._book is None:
            self._book = self.compose()
        return self._book


@dataclass(slots=True)
class TurnLanguage:
    language: str = DEFAULTS.speech.language

    def declare(self, language: str) -> None:
        if language in REPLY_LANGUAGES:
            self.language = language

    def read(self) -> str:
        return self.language


@dataclass(frozen=True, slots=True)
class SpokenChunk:
    text: str
    language: str


@dataclass(frozen=True, slots=True)
class PlanRead:
    structured: bool
    intent: ReplyIntent
    language: str
    replaced: bool
    proactive: bool = False
    claims: tuple[ClaimHeader, ...] = ()


PlanHandler = Callable[[PlanRead], None]
GateHandler = Callable[[SpeechVerdict, ClaimHeader | None], None]


@dataclass(frozen=True, slots=True)
class ReplyHandlers:
    on_intent: IntentHandler | None = None
    on_plan: PlanHandler | None = None
    on_language: Callable[[str], None] | None = None
    on_gate: GateHandler | None = None
    on_sentence: Callable[[int, str, bool], None] | None = None


class ReplyPlanner:
    def __init__(
        self,
        *,
        max_sentences: int,
        language: str,
        handlers: ReplyHandlers | None = None,
        speech_gate: SpeechGate | None = None,
    ) -> None:
        self._max_sentences = max_sentences
        self._language = language
        resolved = handlers if handlers is not None else ReplyHandlers()
        self._on_intent = resolved.on_intent
        self._on_plan = resolved.on_plan
        self._on_language = resolved.on_language
        self._on_gate = resolved.on_gate
        self._on_sentence = resolved.on_sentence
        self._gate = speech_gate
        self._claims: list[ClaimHeader] = []
        self._says: list[str] = []
        self._review: set[int] = set()
        self._verdicts: dict[int, bool] = {}
        self._review_pieces: dict[int, list[SpeechPiece]] = {}
        self._commits_owner = False
        self._blocked: SpeechVerdict | None = None
        self._raw = ""
        self._released: list[int] = []
        self._structured: bool | None = None
        self._declared_language: str | None = None
        self._declared_intent: ReplyIntent | None = None
        self._replacement: str | None = None
        self._lead: int | None = None
        self._waiting: list[SpeechPiece] = []
        self._short_spoken = False
        self._spoken = 0
        self._accepted_sentences: list[tuple[str, bool]] = []
        self._reported = False

    @property
    def claims(self) -> tuple[ClaimHeader, ...]:
        return tuple(self._claims)

    def claim_header(self, claim: int) -> ClaimHeader | None:
        return self._claims[claim] if claim < len(self._claims) else None

    def claim_say(self, claim: int) -> str:
        return self._says[claim] if claim < len(self._says) else ""

    def hold_for_review(self, claim: int) -> None:
        self._review.add(claim)

    def review_claims(self) -> tuple[int, ...]:
        return tuple(sorted(self._review))

    def settle_claim(self, claim: int, *, accepted: bool) -> list[str]:
        self._verdicts[claim] = accepted
        return self._release_review(claim)

    def refuse(self, claim: int) -> None:
        self._block("critic_refused", self.claim_header(claim))

    @property
    def blocked(self) -> SpeechVerdict | None:
        return self._blocked

    @property
    def intent(self) -> ReplyIntent:
        return self._declared_intent or "answer"

    @property
    def explicit(self) -> bool:
        return self._declared_intent is not None

    @property
    def max_sentences(self) -> int:
        return 1 if self.intent in SHORT_INTENTS else self._max_sentences

    @property
    def silent(self) -> bool:
        return self.intent == "silent"

    @property
    def language(self) -> str:
        return self._declared_language or self._language

    def feed(self, piece: str) -> list[SpeechPiece]:
        self._raw += piece
        return self._release(final=False)

    def finish(self) -> list[SpeechPiece]:
        return self._release(final=True)

    def speak(self, reply: str) -> list[str]:
        return self.speak_pieces([*self.feed(reply), *self.finish()])

    def speak_pieces(self, pieces: Sequence[SpeechPiece]) -> list[str]:
        spoken: list[str] = []
        if not self.silent:
            for claim, group in groupby(pieces, key=lambda piece: piece.claim):
                text = "".join(piece.text for piece in group)
                for sentence in split_sentences(text, language=self.language):
                    spoken.extend(self.take_sentence(sentence, claim))
                spoken.extend(self.end_claim(claim))
        return [*spoken, *self.finish_sentences()]

    def take_sentence(self, sentence: str, claim: int = 0) -> list[str]:
        if self.silent or self._blocked is not None:
            return []
        if self._gate is not None:
            verdict = self._judge(self._gate, sentence, claim)
            if verdict != "pass":
                self._block(verdict, self.claim_header(claim))
                return []
        if claim in self._review:
            self._review_pieces.setdefault(claim, []).append(SpeechPiece(claim, sentence))
            return []
        if self._lead is None:
            self._lead = claim
        if claim == self._lead:
            return self._admit(sentence, claim)
        self._waiting = [piece for piece in self._waiting if piece.claim == claim]
        self._waiting.append(SpeechPiece(claim, sentence))
        return []

    def end_claim(self, claim: int) -> list[str]:
        if claim in self._review:
            return self._release_review(claim)
        ended = [piece.text for piece in self._waiting if piece.claim == claim]
        self._waiting = [piece for piece in self._waiting if piece.claim != claim]
        return [line for sentence in ended for line in self._admit(sentence, claim)]

    def _release_review(self, claim: int) -> list[str]:
        verdict = self._verdicts.get(claim)
        if verdict is None:
            return []
        pieces = self._review_pieces.pop(claim, [])
        if not verdict:
            self.refuse(claim)
            return []
        return [line for piece in pieces for line in self._admit(piece.text, piece.claim)]

    def finish_sentences(self) -> list[str]:
        waiting, self._waiting = self._waiting, []
        spoken: list[str] = []
        for claim in sorted(self._review_pieces):
            spoken.extend(self._release_review(claim))
        spoken.extend(line for piece in waiting for line in self._admit(piece.text, piece.claim))
        spoken.extend(self._fallback())
        self._report()
        return spoken

    def _admit(self, sentence: str, claim: int) -> list[str]:
        if self.intent in SHORT_INTENTS:
            if self._short_spoken:
                return []
            self._short_spoken = True
        authorized = self._authorized_question(sentence, claim)
        self._accepted_sentences.append((sentence, authorized))
        if self._on_sentence is not None:
            self._on_sentence(self._spoken, sentence, authorized)
        self._spoken += 1
        return [sentence]

    def authorized_reply_question(self, text: str) -> bool:
        count = len(split_sentences(text, language=self.language))
        retained = self._accepted_sentences[:count]
        if text != " ".join(sentence for sentence, _ in retained):
            return False
        questions = [allowed for sentence, allowed in retained if "?" in sentence]
        return bool(questions) and all(questions)

    def _authorized_question(self, sentence: str, claim: int) -> bool:
        gate, header = self._gate, self.claim_header(claim)
        if gate is None or not gate.verify or gate.proactive or header is None:
            return False
        if header.act != "question" or header.basis != "brief":
            return False
        if self._judge(gate, sentence, claim) != "pass":
            return False
        normalized = " ".join(sentence.split()).casefold()
        return any(
            entry.kind == "question"
            and entry.namespace == "brief"
            and entry.disclosure == "room_speakable"
            and " ".join(entry.text.split()).casefold() == normalized
            for ref in header.refs
            if (entry := gate.book.entries.get(ref)) is not None
        )

    def _judge(self, gate: SpeechGate, sentence: str, claim: int) -> SpeechVerdict:
        if not gate.verify:
            return "pass"
        verdict = check_plan(
            self._claims, commits_owner=self._commits_owner, proactive=gate.proactive
        )
        header = self.claim_header(claim)
        if verdict == "pass" and header is not None:
            verdict = check_claim(header, gate.book)
        if verdict != "pass":
            return verdict
        return check_sentence(sentence, header, gate.book)

    def _block(self, verdict: SpeechVerdict, claim: ClaimHeader | None) -> None:
        if self._blocked is not None:
            return
        self._blocked = verdict
        self._waiting = []
        if self._on_gate is not None:
            self._on_gate(verdict, claim)

    def _fallback(self) -> list[str]:
        gate, locale = self._gate, locale_of(self.language)
        if self._blocked is None or self._spoken or self.silent or gate is None or locale is None:
            return []
        if self._blocked == "owner_commitment":
            self._spoken += 1
            return [speech_line("owner_commitment", locale)]
        if gate.proactive:
            return []
        self._spoken += 1
        return [speech_line("unsure", locale)]

    def _release(self, *, final: bool) -> list[SpeechPiece]:
        if self._structured is None:
            body = self._raw.lstrip()
            if not body:
                return []
            self._structured = body.startswith(JSON_START)
        if not self._structured:
            return self._fresh(0, self._raw)
        if self._blocked == "unparsed_plan":
            return []
        try:
            draft = PLAN_DRAFT.validate_json(
                self._raw.lstrip(),
                experimental_allow_partial=False if final else "trailing-strings",
            )
        except ValidationError:
            self._block("unparsed_plan", None)
            return []
        if "claims" not in draft and not final:
            return []
        self._read_plan(draft)
        if self._replacement is not None:
            return self._fresh(0, self._replacement)
        if final and "claims" not in draft and not self.silent:
            self._block("unparsed_plan", None)
            return []
        return self._claim_pieces(draft.get("claims", []), final=final)

    def _claim_pieces(self, claims: Sequence[_ClaimDraft], *, final: bool) -> list[SpeechPiece]:
        pieces: list[SpeechPiece] = []
        for index, claim in enumerate(claims):
            if index == len(self._released):
                self._released.append(0)
                pieces.append(SpeechPiece(index, ""))
            if "say" not in claim:
                if final:
                    self._block("unparsed_plan", None)
                return pieces
            if index == len(self._claims):
                try:
                    self._claims.append(ClaimHeader.model_validate(claim))
                except ValidationError:
                    self._block("unparsed_plan", None)
                    return pieces
                self._says.append(claim["say"])
            else:
                self._says[index] = claim["say"]
            pieces.extend(self._fresh(index, claim["say"]))
        return pieces

    def _fresh(self, claim: int, text: str) -> list[SpeechPiece]:
        if claim == len(self._released):
            self._released.append(0)
        fresh = text[self._released[claim] :]
        self._released[claim] = len(text)
        return [SpeechPiece(claim, fresh)] if fresh else []

    def _read_plan(self, draft: _PlanDraft) -> None:
        if self._declared_intent is not None:
            return
        self._commits_owner = draft.get("commits_owner", False)
        language = draft.get("language")
        if language is not None:
            self._declared_language = language
            if self._on_language is not None:
                self._on_language(language)
        self._declared_intent = draft.get("intent", "answer")
        if self._on_intent is not None:
            self._replacement = self._on_intent(self.intent)

    def _report(self) -> None:
        if self._reported or self._on_plan is None or self._structured is None:
            return
        self._reported = True
        self._on_plan(
            PlanRead(
                structured=self._structured,
                intent=self.intent,
                language=self.language,
                replaced=self._replacement is not None,
                proactive=self._gate is not None and self._gate.proactive,
                claims=self.claims,
            )
        )


def planned_reply(
    text: str,
    *,
    max_sentences: int,
    language: str,
    handlers: ReplyHandlers | None = None,
    speech_gate: SpeechGate | None = None,
) -> SpokenChunk:
    planner = ReplyPlanner(
        max_sentences=max_sentences,
        language=language,
        handlers=handlers,
        speech_gate=speech_gate,
    )
    return SpokenChunk(text=" ".join(planner.speak(text)), language=planner.language)
