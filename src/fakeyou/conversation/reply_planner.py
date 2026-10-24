import re
from typing import Literal, cast

from fakeyou.foundation.sentences import sentence_end

ReplyIntent = Literal["answer", "ack", "clarify", "decline", "silent"]

INTENT_PREFIX = "intent:"
INTENT_LINE = re.compile(
    r"^intent:[ \t]*(answer|ack|clarify|decline|silent)\b[ \t]*\n?", re.IGNORECASE
)
SHORT_INTENTS = frozenset({"ack", "decline"})
MAX_HEAD_CHARS = 64


class ReplyPlanner:
    def __init__(self, *, max_sentences: int, language: str) -> None:
        self._max_sentences = max_sentences
        self._language = language
        self._head = ""
        self._intent: ReplyIntent | None = None
        self._explicit = False
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
        return self._language

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
        if self._intent is None:
            head = self._head
            stripped = head.lstrip()
            if stripped.casefold().startswith(INTENT_PREFIX):
                match = INTENT_LINE.match(head)
                if match is not None:
                    self._intent = cast(ReplyIntent, match.group(1).casefold())
                    self._explicit = True
                    rest = head[match.end() :]
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


def planned_reply(text: str, *, max_sentences: int, language: str) -> str:
    planner = ReplyPlanner(max_sentences=max_sentences, language=language)
    content = planner.feed(text) + planner.finish()
    if planner.silent:
        return ""
    spoken: list[str] = []
    for sentence in split_sentences(content, language=language):
        spoken.extend(planner.take_sentence(sentence))
    spoken.extend(planner.finish_sentences())
    return " ".join(spoken)
