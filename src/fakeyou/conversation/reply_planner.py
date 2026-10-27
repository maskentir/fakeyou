from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from langchain_core.utils.json import parse_partial_json
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.state_speech import locale_of, speech_line
from fakeyou.foundation.sentences import split_sentences
from fakeyou.session.speech import (
    ContributionKind,
    FactBook,
    SpeechAct,
    SpeechVerdict,
    check_act,
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

REPLY_INTENTS: frozenset[str] = frozenset(get_args(ReplyIntent))
REPLY_LANGUAGES: frozenset[str] = frozenset(get_args(ReplyLanguage))
SHORT_INTENTS = frozenset({"ack", "decline", "stop", "shorter", "louder"})
JSON_START = "{"
CONTRIBUTION_KINDS: frozenset[str] = frozenset(get_args(ContributionKind))
GateOutcome = Literal["pass", "drop", "n/a"]

IntentHandler = Callable[[ReplyIntent], str | None]


class ReplyPlan(BaseModel):
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
            "again, or to speak louder; the text is then only a few words acknowledging it."
        )
    )
    contribution: ContributionKind = Field(
        default="none",
        description=(
            "none for every ordinary reply. Only when nobody asked you anything (a proactive "
            "contribution): fact adds a known fact the room lacks, correction fixes something the "
            "room or screen just stated that conflicts with a known fact, question asks a brief "
            "question that unblocks the discussion."
        ),
    )
    cites: list[str] = Field(
        default_factory=list,
        description=(
            "The id of every brief item, profile fact (fact-<n>) or recalled memory fact that "
            "your text states or relies on. Empty when the text states no such fact. A "
            "contribution must cite at least one id, and a question must cite a brief question."
        ),
    )
    commits_owner: bool = Field(
        default=False,
        description=(
            "True when the text would agree, approve, promise, accept a deadline or price, or "
            "decide anything on the owner's behalf."
        ),
    )
    text: str = Field(
        description=(
            "The words you speak aloud, every word in the language you declared: English when "
            "language is en, Indonesian when it is id, even when your profile and the meeting use "
            "the other one. One or two short sentences. Empty when the intent is silent."
        )
    )


@dataclass(slots=True)
class SpeechGate:
    compose: Callable[[], FactBook]
    proactive: bool = False
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
    contribution: str = "none"
    cites: tuple[str, ...] = ()


PlanHandler = Callable[[PlanRead], None]


@dataclass(frozen=True, slots=True)
class ReplyHandlers:
    on_intent: IntentHandler | None = None
    on_plan: PlanHandler | None = None
    on_language: Callable[[str], None] | None = None
    on_gate: Callable[[SpeechVerdict, SpeechAct], None] | None = None


def _act_of(plan: dict[str, object]) -> SpeechAct:
    cites = plan.get("cites")
    contribution = plan.get("contribution")
    return SpeechAct(
        cites=tuple(item for item in cites if isinstance(item, str))
        if isinstance(cites, list)
        else (),
        commits_owner=plan.get("commits_owner") is True,
        contribution=cast(ContributionKind, contribution)
        if isinstance(contribution, str) and contribution in CONTRIBUTION_KINDS
        else "none",
    )


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
        self._gate = speech_gate
        self._act: SpeechAct | None = None
        self._blocked: SpeechVerdict | None = None
        self._pending: list[str] = []
        self._raw = ""
        self._released = 0
        self._structured: bool | None = None
        self._declared_language: str | None = None
        self._declared_intent: ReplyIntent | None = None
        self._replacement: str | None = None
        self._held: str | None = None
        self._short_spoken = False
        self._spoken = 0
        self._reported = False

    @property
    def act(self) -> SpeechAct | None:
        return self._act

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

    def feed(self, piece: str) -> str:
        self._raw += piece
        return self._release(final=False)

    def finish(self) -> str:
        return self._release(final=True)

    def take_sentence(self, sentence: str) -> list[str]:
        if self.silent:
            return []
        if self._gate is None:
            return self._admit(sentence)
        self._pending.append(sentence)
        return self._drain()

    def finish_sentences(self) -> list[str]:
        if self._gate is not None and self._act is None:
            self._act = self._final_act()
        spoken = self._drain() if self._gate is not None else []
        if self._held is not None:
            held, self._held = self._held, None
            if not (self.intent == "answer" and self._spoken):
                self._spoken += 1
                spoken.append(held)
        return [*spoken, *self._fallback()]

    def _final_act(self) -> SpeechAct:
        plan = parse_partial_json(self._raw.lstrip()) if self._structured else None
        return _act_of(plan) if isinstance(plan, dict) else SpeechAct()

    def _admit(self, sentence: str) -> list[str]:
        if self.intent in SHORT_INTENTS:
            if self._short_spoken:
                return []
            self._short_spoken = True
            self._spoken += 1
            return [sentence]
        if self.explicit and self.intent == "answer" and sentence.rstrip().endswith("?"):
            self._held = sentence
            return []
        spoken: list[str] = []
        if self._held is not None:
            spoken.append(self._held)
            self._held = None
        spoken.append(sentence)
        self._spoken += len(spoken)
        return spoken

    def _drain(self) -> list[str]:
        act, gate = self._act, self._gate
        if act is None or gate is None:
            return []
        pending, self._pending = self._pending, []
        spoken: list[str] = []
        for sentence in pending:
            verdict = self._blocked or self._judge(act, gate, sentence)
            if verdict != "pass":
                self._block(verdict, act)
                break
            spoken.extend(self._admit(sentence))
        return spoken

    def _judge(self, act: SpeechAct, gate: SpeechGate, sentence: str) -> SpeechVerdict:
        verdict = check_act(act, gate.book)
        if verdict == "pass" and gate.proactive and act.contribution == "none":
            verdict = "uncited_contribution"
        if verdict != "pass":
            return verdict
        return check_sentence(sentence, act, gate.book)

    def _block(self, verdict: SpeechVerdict, act: SpeechAct) -> None:
        if self._blocked is not None:
            return
        self._blocked = verdict
        self._held = None
        if self._on_gate is not None:
            self._on_gate(verdict, act)

    def _fallback(self) -> list[str]:
        gate, locale = self._gate, locale_of(self.language)
        if self._blocked is None or self._spoken or gate is None or locale is None:
            return []
        if self._blocked == "owner_commitment":
            self._spoken += 1
            return [speech_line("owner_commitment", locale)]
        if gate.proactive:
            return []
        self._spoken += 1
        return [speech_line("unsure", locale)]

    def _release(self, *, final: bool) -> str:
        if self._structured is None:
            body = self._raw.lstrip()
            if not body:
                return ""
            self._structured = body.startswith(JSON_START)
        if not self._structured:
            if self._act is None:
                self._act = SpeechAct()
            self._report(structured=False)
            return self._take_text(self._raw)
        plan = parse_partial_json(self._raw.lstrip())
        if not isinstance(plan, dict):
            if final and self._act is None:
                self._act = SpeechAct()
            return ""
        if self._act is None and ("text" in plan or final) and ("cites" in plan or final):
            self._act = _act_of(plan)
        if not self._read_plan(plan, final=final):
            return ""
        text = self._replacement if self._replacement is not None else plan.get("text")
        return self._take_text(text) if isinstance(text, str) else ""

    def _read_plan(self, plan: dict[str, object], *, final: bool) -> bool:
        if self._declared_intent is not None:
            return True
        if "text" not in plan and not final:
            return False
        language = plan.get("language")
        if isinstance(language, str) and language in REPLY_LANGUAGES:
            self._declared_language = language
            if self._on_language is not None:
                self._on_language(language)
        intent = plan.get("intent")
        known = isinstance(intent, str) and intent in REPLY_INTENTS
        self._declared_intent = cast(ReplyIntent, intent) if known else "answer"
        if self._on_intent is not None:
            self._replacement = self._on_intent(self.intent)
        self._report(structured=True, plan=plan)
        return True

    def _report(self, *, structured: bool, plan: dict[str, object] | None = None) -> None:
        if self._reported or self._on_plan is None:
            return
        self._reported = True
        act = _act_of(plan) if plan is not None else SpeechAct()
        self._on_plan(
            PlanRead(
                structured=structured,
                intent=self.intent,
                language=self.language,
                replaced=self._replacement is not None,
                contribution=act.contribution,
                cites=act.cites,
            )
        )

    def _take_text(self, text: str) -> str:
        fresh = text[self._released :]
        self._released = len(text)
        return fresh


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
    content = planner.feed(text) + planner.finish()
    if planner.silent:
        return SpokenChunk(text="", language=planner.language)
    spoken: list[str] = []
    for sentence in split_sentences(content, language=planner.language):
        spoken.extend(planner.take_sentence(sentence))
    spoken.extend(planner.finish_sentences())
    return SpokenChunk(text=" ".join(spoken), language=planner.language)
