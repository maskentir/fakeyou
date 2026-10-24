from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast, get_args

from langchain_core.utils.json import parse_partial_json
from pydantic import BaseModel, ConfigDict, Field

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

    language: ReplyLanguage = Field(
        description=(
            "The language of the participant's last line: en when its words are mostly English, "
            "id when they are mostly Indonesian. Names, the meeting topic, the speaking style and "
            "the default language do not count. The spoken text uses this language."
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
            "The words you speak aloud, in the chosen language, one or two short sentences. Empty "
            "when the intent is silent."
        )
    )


@dataclass(frozen=True, slots=True)
class SpokenChunk:
    text: str
    language: str


class ReplyPlanner:
    def __init__(
        self,
        *,
        max_sentences: int,
        language: str,
        on_intent: IntentHandler | None = None,
    ) -> None:
        self._max_sentences = max_sentences
        self._language = language
        self._on_intent = on_intent
        self._raw = ""
        self._released = 0
        self._structured: bool | None = None
        self._declared_language: str | None = None
        self._declared_intent: ReplyIntent | None = None
        self._replacement: str | None = None
        self._held: str | None = None
        self._short_spoken = False
        self._spoken = 0

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
        if not self._structured:
            return self._take_text(self._raw)
        plan = parse_partial_json(self._raw.lstrip())
        if not isinstance(plan, dict) or not self._read_plan(plan, final=final):
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
        intent = plan.get("intent")
        known = isinstance(intent, str) and intent in REPLY_INTENTS
        self._declared_intent = cast(ReplyIntent, intent) if known else "answer"
        if self._on_intent is not None:
            self._replacement = self._on_intent(self.intent)
        return True

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
) -> SpokenChunk:
    planner = ReplyPlanner(max_sentences=max_sentences, language=language, on_intent=on_intent)
    content = planner.feed(text) + planner.finish()
    if planner.silent:
        return SpokenChunk(text="", language=planner.language)
    spoken: list[str] = []
    for sentence in split_sentences(content, language=planner.language):
        spoken.extend(planner.take_sentence(sentence))
    spoken.extend(planner.finish_sentences())
    return SpokenChunk(text=" ".join(spoken), language=planner.language)
