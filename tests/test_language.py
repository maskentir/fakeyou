import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

from fakeyou.foundation.language import SpokenLanguage, function_words, spoken_language

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


LANGUAGE_SCHEMAS = (
    "import importlib, json, sys; importlib.import_module(sys.argv[1]); "
    "from fakeyou.conversation.reply_planner import SpeechPlan; "
    "from fakeyou.conversation.turn_judge import TurnJudgement; "
    "from fakeyou.memory.persona import PersonaProfile; "
    "json.dump([model.model_json_schema()['properties']['language'] "
    "for model in (TurnJudgement, SpeechPlan, PersonaProfile)], sys.stdout)"
)
FIRST_IMPORTS = (
    "fakeyou.worker.__main__",
    "fakeyou.cli.main",
    "fakeyou.orchestration.runtime",
    "fakeyou.foundation.language",
    "fakeyou.conversation.turn_judge",
)


def _language_schemas(first_import: str) -> str:
    return subprocess.run(
        [sys.executable, "-c", LANGUAGE_SCHEMAS, first_import],
        capture_output=True,
        check=True,
        text=True,
    ).stdout


def test_language_schemas_do_not_depend_on_what_a_process_imports_first() -> None:
    schemas = {first: _language_schemas(first) for first in FIRST_IMPORTS}

    assert len(set(schemas.values())) == 1
    assert '"enum": ["id", "en"]' in schemas["fakeyou.worker.__main__"]


def _language_literals(path: Path) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Literal"
        and isinstance(node.slice, ast.Tuple)
        and {getattr(item, "value", None) for item in node.slice.elts}
        == set(get_args(SpokenLanguage))
    ]


def test_the_spoken_languages_are_spelled_once() -> None:
    root = Path(__file__).parent.parent
    spelled = {
        str(path.relative_to(root)): lines
        for folder in ("src", "evals", "tests")
        for path in (root / folder).rglob("*.py")
        if (lines := _language_literals(path))
    }

    assert spelled == {"src/fakeyou/foundation/language.py": [8]}
