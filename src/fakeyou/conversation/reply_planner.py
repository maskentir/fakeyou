from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from langchain_core.utils.json import parse_partial_json
from pydantic import BaseModel, ConfigDict, Field

from fakeyou.config.settings import DEFAULTS
from fakeyou.conversation.contribution import (
    ContributionClaim,
    ContributionGate,
    ContributionKind,
    ContributionSource,
    GateOutcome,
    GateVerdict,
    validate_contribution,
)
from fakeyou.foundation.sentences import split_sentences

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
    text: str = Field(
        description=(
            "The words you speak aloud, every word in the language you declared: English when "
            "language is en, Indonesian when it is id, even when your profile and the meeting use "
            "the other one. One or two short sentences. Empty when the intent is silent."
        )
    )
    contribution_kind: ContributionKind | None = Field(
        default=None,
        description=(
            "Only when nobody asked you anything (a proactive contribution): fact adds a known "
            "fact the room lacks, correction fixes something the room or screen just stated that "
            "conflicts with a known fact, question asks a brief question that unblocks the "
            "discussion. Leave unset for ordinary replies."
        ),
    )
    contribution_source: ContributionSource | None = Field(
        default=None,
        description=(
            "Where a proactive contribution comes from: profile (owner profile verified facts), "
            "brief (meeting brief approved facts, questions or objectives), or memory (recalled "
            "earlier-meeting evidence)."
        ),
    )
    source_ref: str | None = Field(
        default=None,
        description=(
            "The id of the brief item used (brief), fact-<n> for the n-th profile verified fact "
            "(profile), or the recalled fact id (memory). Required with contribution_kind."
        ),
    )


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
    kind: str | None = None
    source: str | None = None
    source_ref: str | None = None
    gate: GateOutcome = "n/a"
    gate_reason: str = ""


PlanHandler = Callable[[PlanRead], None]


@dataclass(frozen=True, slots=True)
class ReplyHandlers:
    on_intent: IntentHandler | None = None
    on_plan: PlanHandler | None = None
    on_language: Callable[[str], None] | None = None


class ReplyPlanner:
    def __init__(
        self,
        *,
        max_sentences: int,
        language: str,
        handlers: ReplyHandlers | None = None,
        contribution_gate: ContributionGate | None = None,
    ) -> None:
        self._max_sentences = max_sentences
        self._language = language
        resolved = handlers if handlers is not None else ReplyHandlers()
        self._on_intent = resolved.on_intent
        self._on_plan = resolved.on_plan
        self._on_language = resolved.on_language
        self._gate = contribution_gate
        self._verdict: GateVerdict | None = None
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
    def verdict(self) -> GateVerdict | None:
        return self._verdict

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

    def finish_sentences(self) -> list[str]:
        if self._held is None:
            return []
        held, self._held = self._held, None
        if self.intent == "answer" and self._spoken:
            return []
        self._spoken += 1
        return [held]

    def _release(self, *, final: bool) -> str:
        if self._structured is None:
            body = self._raw.lstrip()
            if not body:
                return ""
            self._structured = body.startswith(JSON_START)
        if self._gate is not None:
            return self._release_gated(final=final)
        if not self._structured:
            self._report(structured=False)
            return self._take_text(self._raw)
        plan = parse_partial_json(self._raw.lstrip())
        if not isinstance(plan, dict) or not self._read_plan(plan, final=final):
            return ""
        text = self._replacement if self._replacement is not None else plan.get("text")
        return self._take_text(text) if isinstance(text, str) else ""

    def _release_gated(self, *, final: bool) -> str:
        if not final:
            return ""
        gate = self._gate
        assert gate is not None
        plan = parse_partial_json(self._raw.lstrip()) if self._structured else None
        if not isinstance(plan, dict):
            self._verdict = validate_contribution(
                ContributionClaim(structured=False, intent="answer", text=self._raw), gate
            )
            self._declared_intent = "silent"
            self._report(structured=False)
            return ""
        self._read_plan(plan, final=True)
        raw_text = plan.get("text")
        text = self._replacement if self._replacement is not None else raw_text
        text = text if isinstance(text, str) else ""
        kind = plan.get("contribution_kind")
        source = plan.get("contribution_source")
        source_ref = plan.get("source_ref")
        self._verdict = validate_contribution(
            ContributionClaim(
                structured=True,
                intent=self.intent,
                text=text,
                kind=kind if isinstance(kind, str) else None,
                source=source if isinstance(source, str) else None,
                source_ref=source_ref if isinstance(source_ref, str) else None,
            ),
            gate,
        )
        self._report(structured=True)
        if not self._verdict.passed:
            self._declared_intent = "silent"
            return ""
        return self._take_text(text)

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
        self._report(structured=True)
        return True

    def _report(self, *, structured: bool) -> None:
        if self._reported or self._on_plan is None:
            return
        if self._gate is not None and self._verdict is None:
            return
        self._reported = True
        verdict = self._verdict
        gate: GateOutcome = "n/a"
        if verdict is not None:
            gate = "drop" if not verdict.passed else ("pass" if verdict.kind else "n/a")
        self._on_plan(
            PlanRead(
                structured=structured,
                intent=self.intent,
                language=self.language,
                replaced=self._replacement is not None,
                kind=verdict.kind if verdict is not None else None,
                source=verdict.source if verdict is not None else None,
                source_ref=verdict.source_ref if verdict is not None else None,
                gate=gate,
                gate_reason=verdict.reason if verdict is not None else "",
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
    on_intent: IntentHandler | None = None,
    contribution_gate: ContributionGate | None = None,
) -> SpokenChunk:
    planner = ReplyPlanner(
        max_sentences=max_sentences,
        language=language,
        handlers=ReplyHandlers(on_intent=on_intent),
        contribution_gate=contribution_gate,
    )
    content = planner.feed(text) + planner.finish()
    if planner.silent:
        return SpokenChunk(text="", language=planner.language)
    spoken: list[str] = []
    for sentence in split_sentences(content, language=planner.language):
        spoken.extend(planner.take_sentence(sentence))
    spoken.extend(planner.finish_sentences())
    return SpokenChunk(text=" ".join(spoken), language=planner.language)
