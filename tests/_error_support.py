import re

from fakeyou.catalog.errors import CATALOG, ErrorCode


def message_pattern(code: ErrorCode) -> str:
    parts = re.split(r"\{[^}]*\}", CATALOG[code].template)
    return "^" + ".*".join(re.escape(part) for part in parts)
