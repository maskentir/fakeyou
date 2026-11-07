import json
from pathlib import Path

import pytest

from fakeyou.foundation.language import function_words, spoken_language

CASES = json.loads((Path(__file__).parent / "fixtures" / "language-cases.json").read_text())


@pytest.mark.parametrize("line", CASES["en"])
def test_english_lines_are_detected(line: str) -> None:
    assert spoken_language(line) == "en"


@pytest.mark.parametrize("line", CASES["id"])
def test_indonesian_lines_are_detected(line: str) -> None:
    assert spoken_language(line) == "id"


@pytest.mark.parametrize("line", CASES["undecided"])
def test_lines_without_a_clear_language_stay_undecided(line: str) -> None:
    assert spoken_language(line) is None


def test_the_two_function_word_lists_do_not_overlap() -> None:
    lists = function_words()

    assert not lists["en"] & lists["id"]
