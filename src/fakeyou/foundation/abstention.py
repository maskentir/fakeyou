from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import cache
from typing import Literal

from fakeyou.foundation.resources import read_package_text

AbstentionId = Literal["AB1", "AB2", "AB3"]
CATALOG_ASSET = "publication_catalog.json"
DEFAULT_LANGUAGE = "en"
NOT_IN_CATALOG = "abstention_not_in_catalog"


@dataclass(frozen=True, slots=True)
class AbstentionRef:
    entry_id: str
    version: int
    language: str
    text_digest: str


@dataclass(frozen=True, slots=True)
class AbstentionEntry:
    entry_id: str
    version: int
    language: str
    scope: str
    text: str

    @property
    def ref(self) -> AbstentionRef:
        return AbstentionRef(
            self.entry_id, self.version, self.language, abstention_digest(self.text)
        )


def abstention_digest(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).casefold().encode()).hexdigest()


@cache
def _catalog() -> dict[tuple[str, int, str], AbstentionEntry]:
    raw = json.loads(read_package_text("assets", CATALOG_ASSET))
    version = int(raw["version"])
    return {
        (entry_id, version, language): AbstentionEntry(
            entry_id, version, language, str(entry["scope"]), str(text)
        )
        for entry_id, entry in raw["abstentions"].items()
        for language, text in entry["text"].items()
    }


def catalog_version() -> int:
    return next(iter(_catalog().values())).version


def catalog_languages() -> frozenset[str]:
    return frozenset(language for _, _, language in _catalog())


def abstention_entry(entry_id: str, language: str | None) -> AbstentionEntry:
    spoken = language if language in catalog_languages() else DEFAULT_LANGUAGE
    return _catalog()[entry_id, catalog_version(), spoken]


def catalog_entry(ref: AbstentionRef) -> AbstentionEntry | None:
    return _catalog().get((ref.entry_id, ref.version, ref.language))


def select_abstention(
    *, look_status: str | None, frame_inspected: bool, language: str | None
) -> AbstentionEntry:
    if look_status == "insufficient" and frame_inspected:
        return abstention_entry("AB2", language)
    if look_status in {"insufficient", "unavailable"}:
        return abstention_entry("AB3", language)
    return abstention_entry("AB1", language)


def abstention_refusal(ref: AbstentionRef | None, scope: str) -> str | None:
    if ref is None:
        return None
    entry = catalog_entry(ref)
    if entry is None or entry.scope != scope or abstention_digest(entry.text) != ref.text_digest:
        return NOT_IN_CATALOG
    return None
