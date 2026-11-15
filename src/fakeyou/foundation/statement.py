from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from functools import cache
from typing import Literal

from fakeyou.foundation.abstention import CATALOG_ASSET
from fakeyou.foundation.resources import read_package_text

Tense = Literal["current", "historical"]
StatementShape = Literal["entity_attribute_value", "entity_status", "item_presence"]
NOT_RENDERED = "statement_not_rendered"
STATEMENT_FIELDS: dict[str, tuple[str, ...]] = {
    "entity_attribute_value": ("entity", "attribute", "value"),
    "entity_status": ("entity", "status"),
    "item_presence": ("item",),
}


@dataclass(frozen=True, slots=True)
class FieldSpan:
    name: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Rendering:
    pattern_id: str
    version: int
    tense: Tense
    language: str
    shape: StatementShape
    fields: tuple[FieldSpan, ...]
    text_digest: str


@cache
def patterns() -> tuple[int, dict[tuple[str, str, str], str]]:
    raw = json.loads(read_package_text("assets", CATALOG_ASSET))
    return int(raw["version"]), {
        (tense, shape, language): str(text)
        for tense, shapes in raw["patterns"].items()
        for shape, languages in shapes.items()
        for language, text in languages.items()
    }


def statement_digest(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode()).hexdigest()


def pattern_text(tense: str, shape: str, language: str) -> str | None:
    return patterns()[1].get((tense, shape, language))


def rerender(rendering: Rendering, answer: str) -> str | None:
    version, known = patterns()
    pattern = known.get((rendering.tense, rendering.shape, rendering.language))
    if pattern is None or rendering.version != version:
        return None
    values = {span.name: answer[span.start : span.end] for span in rendering.fields}
    if set(values) != set(STATEMENT_FIELDS[rendering.shape]):
        return None
    return pattern.format(**values)


def rendering_refusal(rendering: Rendering | None, scope: str, text_digest: str) -> str | None:
    if rendering is None:
        return NOT_RENDERED
    version, known = patterns()
    listed = (rendering.tense, rendering.shape, rendering.language) in known
    if not listed or rendering.version != version or rendering.tense != scope:
        return NOT_RENDERED
    if rendering.text_digest != text_digest:
        return NOT_RENDERED
    return None
