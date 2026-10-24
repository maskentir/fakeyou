import re
from collections.abc import Callable
from functools import cache

from sentencex import segment

QUOTED_WORD_END = re.compile(r"(?<!\w)([\"'“‘«‹„‟「『]+)[\w.]+\.$")
LATIN_SENTENCE_ENDINGS = frozenset({".", "!", "?", ";", "…"})
SENTENCE_ENDINGS = frozenset(
    {
        *LATIN_SENTENCE_ENDINGS,
        "。",
        "？",
        "！",
        "；",
        "．",
        "｡",
        "।",
        "॥",
        "؟",
        "؛",
        "۔",
        "؏",
        "၊",
        "။",
        "។",
        "៕",
        "໌",
        "།",
        "༎",
        "։",
        "՜",
        "՞",
        "።",
        "፧",
        "፨",
    }
)
UNAMBIGUOUS_SENTENCE_ENDINGS = SENTENCE_ENDINGS - LATIN_SENTENCE_ENDINGS


def tokenizer_language(language: str | None) -> str:
    if not isinstance(language, str):
        return "en"
    normalized = language.strip().lower().replace("_", "-").split("-")[0]
    return "en" if normalized in ("", "auto") else normalized


@cache
def _tokenizer(language: str) -> Callable[[str], list[str]]:
    def tokenize(text: str) -> list[str]:
        sentences: list[str] = []
        pending = ""
        for span in segment(language, text):
            pending += span
            if span.strip():
                sentence = pending.rstrip()
                sentences.append(sentence)
                pending = pending[len(sentence) :]
        return sentences

    return tokenize


def sentence_end(text: str, *, language: str | None = None) -> int:
    text = text.rstrip()
    if not text:
        return 0
    code = tokenizer_language(language)
    sentences = _tokenizer(code)(text)
    if not sentences:
        return 0
    first_sentence = sentences[0]
    tokenizer_text = text
    if len(sentences) > 1 and (quote := QUOTED_WORD_END.search(first_sentence)):
        start, end = quote.span(1)
        tokenizer_text = text[:start] + " " * (end - start) + text[end:]
        sentences = _tokenizer(code)(tokenizer_text)
        first_sentence = sentences[0]
    if len(sentences) == 1 and first_sentence == tokenizer_text:
        if text[-1] in SENTENCE_ENDINGS:
            return len(text)
        for index, character in enumerate(text):
            if character in UNAMBIGUOUS_SENTENCE_ENDINGS:
                return index + 1
        return 0
    if len(sentences) > 1:
        return len(first_sentence)
    return 0


def split_sentences(text: str, *, language: str | None = None) -> list[str]:
    sentences: list[str] = []
    remaining = text.strip()
    while remaining:
        boundary = sentence_end(remaining, language=language) or len(remaining)
        sentences.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    return [sentence for sentence in sentences if sentence]
