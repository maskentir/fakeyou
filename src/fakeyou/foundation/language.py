import json
import re
from functools import cache
from typing import Literal

from fakeyou.foundation.resources import read_package_text

SpokenLanguage = Literal["en", "id"]
WORD = re.compile(r"\w+")


@cache
def function_words() -> dict[SpokenLanguage, frozenset[str]]:
    lists = json.loads(read_package_text("assets", "function_words.json"))
    return {"en": frozenset(lists["en"]), "id": frozenset(lists["id"])}


def spoken_language(text: str) -> SpokenLanguage | None:
    words = WORD.findall(text.casefold())
    lists = function_words()
    english = sum(word in lists["en"] for word in words)
    indonesian = sum(word in lists["id"] for word in words)
    if english == indonesian:
        return None
    return "en" if english > indonesian else "id"
