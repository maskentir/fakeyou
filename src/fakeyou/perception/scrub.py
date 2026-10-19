import re

INTERNAL_REF = re.compile(r"[\[(]?\b(?:screen|chat|person)#\d+\b[\])]?", re.IGNORECASE)
ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)
SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
REPEATED_PUNCTUATION = re.compile(r"([,;:])\s*(?=[,;:.!?])")
REPEATED_SPACE = re.compile(r"[ \t]{2,}")


def scrub_internal_refs(text: str) -> str:
    cleaned = INTERNAL_REF.sub("", text)
    cleaned = ISO_TIMESTAMP.sub("", cleaned)
    cleaned = SPACE_BEFORE_PUNCTUATION.sub(r"\1", cleaned)
    cleaned = REPEATED_PUNCTUATION.sub("", cleaned)
    cleaned = REPEATED_SPACE.sub(" ", cleaned)
    return cleaned.strip()


def mentions_internal_ref(text: str) -> bool:
    return bool(INTERNAL_REF.search(text) or ISO_TIMESTAMP.search(text))
