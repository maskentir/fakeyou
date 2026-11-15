from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fakeyou.foundation.claims import (
    SHAPE_FIELDS,
    Claim,
    LanguageMarkers,
    Span,
    VerifiedClaim,
    claim_languages,
    has_marker,
    markers_for,
)
from fakeyou.foundation.statement import (
    FieldSpan,
    Rendering,
    Tense,
    pattern_text,
    patterns,
    statement_digest,
)

RENDER_UNSUPPORTED = "claim_render_unsupported"
MAX_FIELD_WORDS = 8
MAX_FIELD_CODE_POINTS = 80
CLAUSE_PUNCTUATION = re.compile(r"[;!?\n\r]|[.,:](?:\s|$)")
QUOTED = re.compile(r'^"[^"]+"$')
NUMERIC_TOKEN = re.compile(r"^[$€£]?[A-Za-z]{0,3}\.?\d[\d.,:/\-]*%?[A-Za-z]{0,3}$")


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    rendering: Rendering
    claim: Claim


def _field_refusal(name: str, text: str, markers: LanguageMarkers) -> bool:
    normalized = unicodedata.normalize("NFC", text)
    if not normalized.strip() or normalized != normalized.strip():
        return True
    if len(normalized.split()) > MAX_FIELD_WORDS or len(normalized) > MAX_FIELD_CODE_POINTS:
        return True
    if CLAUSE_PUNCTUATION.search(normalized) or "{" in normalized or "}" in normalized:
        return True
    if has_marker(normalized, markers.negation + markers.temporal + markers.certainty):
        return True
    return name == "value" and not _is_value(normalized, markers)


def _is_value(text: str, markers: LanguageMarkers) -> bool:
    if QUOTED.match(text):
        return True
    tokens = text.split()
    numeric = [bool(NUMERIC_TOKEN.match(token)) for token in tokens]
    words = all(
        known or token.casefold().strip(".") in markers.value_words
        for token, known in zip(tokens, numeric, strict=True)
    )
    return any(numeric) and words


def render(verified: VerifiedClaim, tense: Tense, language: str | None) -> Rendered | None:
    claim, support, answer = verified.claim, verified.support, verified.answer
    if not verified.supported or answer is None or claim.polarity != "positive":
        return None
    if language not in claim_languages():
        return None
    if tense == "current" and claim.source != "screen":
        return None
    pattern = pattern_text(tense, claim.shape, language)
    if pattern is None:
        return None
    markers = markers_for(language)
    spans = tuple(_field_span(name, support.spans.get(name)) for name in SHAPE_FIELDS[claim.shape])
    if any(span is None for span in spans):
        return None
    fields = tuple(span for span in spans if span is not None)
    values = {span.name: answer[span.start : span.end] for span in fields}
    if any(_field_refusal(name, text, markers) for name, text in values.items()):
        return None
    text = pattern.format(**values)
    return Rendered(
        text,
        Rendering(
            pattern_id=f"{tense}.{claim.shape}",
            version=patterns()[0],
            tense=tense,
            language=language,
            shape=claim.shape,
            fields=fields,
            text_digest=statement_digest(text),
        ),
        claim,
    )


def _field_span(name: str, span: Span | None) -> FieldSpan | None:
    return None if span is None else FieldSpan(name, span.start, span.end)
