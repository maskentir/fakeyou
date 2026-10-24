import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from fakeyou.foundation.sentences import sentence_end

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

INTENT_PREFIX = "intent:"
INTENT_LINE = re.compile(
    r"^intent:[ \t]*(answer|ack|clarify|decline|silent|stop|shorter|repeat|louder)\b",
    re.IGNORECASE,
)
LANG_LINE = re.compile(r"^[ \t]*lang:[ \t]*(id|en)\b", re.IGNORECASE)
SHORT_INTENTS = frozenset({"ack", "decline", "stop", "shorter", "louder"})
CONTROL_INTENTS = frozenset({"stop", "shorter", "repeat", "louder"})
MAX_HEAD_CHARS = 64

IntentHandler = Callable[["ReplyIntent"], "str | None"]


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
        self._declared_language: str | None = None
        self._on_intent = on_intent
        self._head = ""
        self._intent: ReplyIntent | None = None
        self._explicit = False
        self._replaced = False
        self._held: str | None = None
        self._short_spoken = False
        self._spoken = 0

    @property
    def intent(self) -> ReplyIntent:
        return self._intent if self._intent is not None else "answer"

    @property
    def explicit(self) -> bool:
        return self._explicit

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
        self._head += piece
        return self._release()

    def finish(self) -> str:
        if self._intent is None:
            self._intent = "answer"
        return self._release()

    def take_sentence(self, sentence: str) -> list[str]:
        if self.silent:
            return []
        if self.intent in SHORT_INTENTS:
            if self._short_spoken:
                return []
            self._short_spoken = True
            self._spoken += 1
            return [sentence]
        if self._explicit and self.intent == "answer" and sentence.rstrip().endswith("?"):
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

    def _release(self) -> str:
        if self._replaced:
            self._head = ""
            return ""
        if self._intent is None:
            head = self._head
            stripped = head.lstrip()
            if stripped.casefold().startswith(INTENT_PREFIX):
                match = INTENT_LINE.match(head)
                if match is not None:
                    intent = cast(ReplyIntent, match.group(1).casefold())
                    language, rest = _read_language(head[match.end() :])
                    self._intent = intent
                    self._explicit = True
                    self._declared_language = language
                    replacement = self._on_intent(self._intent) if self._on_intent else None
                    if replacement is not None:
                        self._replaced = True
                        self._head = ""
                        return replacement
                    self._head = ""
                    return rest
                if "\n" not in head and len(head) <= MAX_HEAD_CHARS:
                    return ""
                self._intent = "answer"
                self._head = _drop_first_line(head)
            elif stripped and INTENT_PREFIX.startswith(stripped.casefold()):
                return ""
            else:
                self._intent = "answer"
        released, self._head = self._head, ""
        return released


def _read_language(rest: str) -> tuple[str | None, str]:
    trimmed = rest.lstrip(" \t")
    match = LANG_LINE.match(trimmed)
    if match is not None:
        return match.group(1).casefold(), trimmed[match.end() :].lstrip("\r\n")
    after_break = trimmed.lstrip("\r\n")
    match = LANG_LINE.match(after_break)
    if match is not None:
        return match.group(1).casefold(), after_break[match.end() :].lstrip("\r\n")
    return None, after_break


def _drop_first_line(head: str) -> str:
    if "\n" in head:
        return head.split("\n", 1)[1]
    return ""


def split_sentences(text: str, *, language: str) -> list[str]:
    sentences: list[str] = []
    remaining = text.strip()
    while remaining:
        boundary = sentence_end(remaining, language=language)
        if not boundary:
            sentences.append(remaining)
            break
        sentences.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    return [sentence for sentence in sentences if sentence]


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
