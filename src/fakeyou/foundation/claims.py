from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from fakeyou.foundation.gathered_evidence import GatheredEvidence
from fakeyou.foundation.publication import EvidenceBinding
from fakeyou.foundation.resources import read_package_text

ClaimShape = Literal["entity_attribute_value", "entity_status", "item_presence"]
ClaimSource = Literal["screen", "camera"]
ClaimDependence = Literal["content", "source", "subject"]
FieldName = Literal["entity", "attribute", "value", "status", "item"]
LANGUAGE_ASSET = "claim_language.json"
CLAIMS_MARKER = "CLAIMS:"
DEFAULT_LANGUAGE = "en"
SHAPE_FIELDS: Mapping[ClaimShape, tuple[FieldName, ...]] = {
    "entity_attribute_value": ("entity", "attribute", "value"),
    "entity_status": ("entity", "status"),
    "item_presence": ("item",),
}
SENTENCE_BREAK = re.compile(r"(?<=[.;!?])\s+|\n+")
VALUE_TOKEN = re.compile(r"\S*\d\S*")
WORD_EDGE_BEFORE = r"(?<![0-9A-Za-z$€£])"
WORD_EDGE_AFTER = r"(?![0-9A-Za-z%])"


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    kind: Literal["screen", "participant"]
    shape: ClaimShape
    entity: str | None = None
    attribute: str | None = None
    value: str | None = None
    status: str | None = None
    item: str | None = None
    polarity: Literal["positive", "negative"] = "positive"
    dependence: ClaimDependence | None = None

    @property
    def source(self) -> ClaimSource:
        return "screen" if self.kind == "screen" else "camera"

    def field(self, name: FieldName) -> str | None:
        value: str | None = getattr(self, name)
        return value or None


CLAIMS = TypeAdapter(tuple[Claim, ...])


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    text: str


SUPPORTED = "ok"


@dataclass(frozen=True, slots=True)
class Support:
    reason: str
    spans: Mapping[str, Span]
    clause: Span | None = None

    @property
    def supported(self) -> bool:
        return self.reason == SUPPORTED


@dataclass(frozen=True, slots=True)
class LanguageMarkers:
    version: int
    clause: tuple[str, ...]
    negation: tuple[str, ...]
    status: tuple[str, ...]
    temporal: tuple[str, ...]
    certainty: tuple[str, ...]
    contrast: tuple[str, ...]
    coordination: tuple[str, ...]
    value_words: frozenset[str]


@cache
def _languages() -> dict[str, LanguageMarkers]:
    raw = json.loads(read_package_text("assets", LANGUAGE_ASSET))
    version = int(raw["version"])
    return {
        language: LanguageMarkers(
            version,
            tuple(entry["clause_markers"]),
            tuple(entry["negation_markers"]),
            tuple(entry["status_qualifiers"]),
            tuple(entry["temporal_markers"]),
            tuple(entry["certainty_markers"]),
            tuple(entry["contrast_markers"]),
            tuple(entry["coordination_markers"]),
            frozenset(entry["value_words"]),
        )
        for language, entry in raw["languages"].items()
    }


def markers_for(language: str | None) -> LanguageMarkers:
    known = _languages()
    return known[language if language in known else DEFAULT_LANGUAGE]


def claim_languages() -> frozenset[str]:
    return frozenset(_languages())


def parse_claims(text: str) -> tuple[Claim, ...]:
    marker = text.find(CLAIMS_MARKER)
    if marker < 0:
        return ()
    payload = text[marker + len(CLAIMS_MARKER) :].lstrip()
    try:
        raw, _end = json.JSONDecoder().raw_decode(payload)
        return CLAIMS.validate_python(raw)
    except (ValueError, ValidationError):
        return ()


def _find(haystack: str, needle: str, offset: int = 0) -> Span | None:
    words = needle.split()
    if not words:
        return None
    pattern = WORD_EDGE_BEFORE + r"\s+".join(re.escape(word) for word in words) + WORD_EDGE_AFTER
    found = re.search(pattern, haystack, re.IGNORECASE)
    if found is None:
        return None
    return Span(offset + found.start(), offset + found.end(), found.group())


def has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(
        (marker in lowered)
        if "'" in marker or not marker[0].isalnum()
        else _find(lowered, marker) is not None
        for marker in markers
    )


def _coordinated(answer: str, spans: Mapping[str, Span], markers: LanguageMarkers) -> bool:
    ordered = sorted(spans.values(), key=lambda span: span.start)
    return any(
        has_marker(answer[left.end : right.start], markers.coordination)
        for left, right in zip(ordered, ordered[1:], strict=False)
    )


def _spans(text: str, breaks: re.Pattern[str], offset: int = 0) -> list[Span]:
    spans: list[Span] = []
    start = 0
    for found in breaks.finditer(text):
        if text[start : found.start()].strip():
            spans.append(Span(offset + start, offset + found.start(), text[start : found.start()]))
        start = found.end()
    if text[start:].strip():
        spans.append(Span(offset + start, offset + len(text), text[start:]))
    return spans


def _clause_break(markers: LanguageMarkers) -> re.Pattern[str]:
    words = "|".join(re.escape(marker) for marker in markers.clause)
    return re.compile(rf",\s+|\s+(?:{words})\s+", re.IGNORECASE)


def _other_values(clause: Span, value: Span | None) -> int:
    count = 0
    for found in VALUE_TOKEN.finditer(clause.text):
        start = clause.start + found.start()
        inside = value is not None and value.start <= start < value.end
        count += 0 if inside else 1
    return count


def _located(claim: Claim, clause: Span) -> dict[str, Span] | None:
    spans: dict[str, Span] = {}
    for name in SHAPE_FIELDS[claim.shape]:
        found = _find(clause.text, claim.field(name) or "", clause.start)
        if found is None:
            return None
        spans[name] = found
    return spans


def support_claim(
    claim: Claim, answer: str | None, *, status: str | None, language: str | None
) -> Support:
    names = SHAPE_FIELDS[claim.shape]
    if any(claim.field(name) is None for name in names):
        return Support("claim_incomplete", {})
    if status != "produced" or not answer:
        return Support("validation_support_unknown", {})
    markers = markers_for(language)
    decisive = names[-1]
    if _find(answer, claim.field(decisive) or "") is None:
        return Support(_missing_reason(claim, answer, markers), {})
    failure = "claim_relation_unsupported"
    for sentence in _spans(answer, SENTENCE_BREAK):
        for clause in _spans(sentence.text, _clause_break(markers), sentence.start):
            spans = _located(claim, clause)
            if spans is None:
                continue
            reason = (
                "claim_relation_ambiguous"
                if _coordinated(answer, spans, markers)
                else _clause_refusal(claim, clause, spans, markers)
            )
            if reason is None:
                return Support(SUPPORTED, spans, clause)
            failure = reason
    return Support(failure, {})


def _missing_reason(claim: Claim, answer: str, markers: LanguageMarkers) -> str:
    if claim.shape != "entity_status":
        return "claim_untraceable"
    entity = claim.field("entity") or ""
    for sentence in _spans(answer, SENTENCE_BREAK):
        if _find(sentence.text, entity) is None:
            continue
        if has_marker(sentence.text, markers.status + markers.negation):
            return "claim_polarity_unsupported"
    return "claim_untraceable"


def _clause_refusal(
    claim: Claim, clause: Span, spans: Mapping[str, Span], markers: LanguageMarkers
) -> str | None:
    negated = has_marker(clause.text, markers.negation)
    if negated != (claim.polarity == "negative"):
        return "claim_polarity_unsupported"
    if claim.shape == "entity_status" and has_marker(clause.text, markers.status):
        qualifier_is_the_claim = has_marker(claim.field("status") or "", markers.status)
        if not qualifier_is_the_claim:
            return "claim_polarity_unsupported"
    if has_marker(clause.text, markers.contrast):
        return "claim_relation_ambiguous"
    if claim.shape == "entity_attribute_value" and _other_values(clause, spans.get("value")):
        return "claim_relation_ambiguous"
    return None


@dataclass(frozen=True, slots=True)
class VerifiedClaim:
    claim: Claim
    evidence: EvidenceBinding | None
    support: Support
    answer: str | None

    @property
    def supported(self) -> bool:
        return self.support.supported and self.evidence is not None


def verify_claim(
    claim: Claim, evidence: GatheredEvidence | None, *, language: str | None
) -> VerifiedClaim:
    item = (
        None
        if evidence is None
        else next((look for look in evidence.provenance if look.kind == claim.kind), None)
    )
    if evidence is None or item is None:
        return VerifiedClaim(claim, None, Support("claim_evidence_missing", {}), None)
    if item.snapshot is not None and item.snapshot.source != claim.source:
        return VerifiedClaim(claim, item, Support("claim_evidence_mismatch", {}), None)
    payload = evidence.payloads.get(claim.kind)
    answer = payload.get("answer") if isinstance(payload, Mapping) else None
    text = answer if isinstance(answer, str) else None
    return VerifiedClaim(
        claim, item, support_claim(claim, text, status=item.status, language=language), text
    )
