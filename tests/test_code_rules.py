import ast
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PYTHON_FILES = sorted(
    path for folder in ("src", "tests", "evals") for path in (ROOT / folder).rglob("*.py")
)
SOURCE_FILES = sorted((ROOT / "src" / "fakeyou").rglob("*.py"))
INDONESIAN = re.compile(
    r"\b("
    + "|".join(json.loads((ROOT / "tests" / "fixtures" / "indonesian-words.json").read_text()))
    + r")\b",
    re.IGNORECASE,
)
MODULES = {path.stem for path in SOURCE_FILES} | {"cli"}
CROSS_CUTTING_SUITES = {
    "code_rules",
    "documentation",
    "documented_commands",
    "fakes",
    "library_contracts",
    "taxonomy",
}
FORBIDDEN_FACTORY_PREFIXES = ("make_", "construct_", "new_", "get_", "fetch_", "init_")


def _relative(path: Path) -> str:
    return str(path.relative_to(ROOT))


def _string_literals(source: str) -> list[str]:
    return [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


@pytest.mark.parametrize("path", PYTHON_FILES, ids=_relative)
def test_python_files_have_no_comments(path: Path) -> None:
    tokens = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)

    assert [token.string for token in tokens if token.type == tokenize.COMMENT] == []


@pytest.mark.parametrize("path", PYTHON_FILES, ids=_relative)
def test_python_files_have_no_docstrings(path: Path) -> None:
    nodes = [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    assert [getattr(node, "name", "module") for node in nodes if ast.get_docstring(node)] == []


@pytest.mark.parametrize("path", PYTHON_FILES, ids=_relative)
def test_python_files_contain_no_indonesian(path: Path) -> None:
    source = path.read_text()
    words = {
        match.lower()
        for literal in [*_string_literals(source), *re.findall(r"\b\w+\b", source)]
        for match in INDONESIAN.findall(literal)
    }

    assert words == set()


@pytest.mark.parametrize(
    "path", sorted((ROOT / "tests").glob("test_*.py")), ids=lambda path: path.name
)
def test_test_files_are_named_after_a_module_or_a_cross_cutting_suite(path: Path) -> None:
    name = path.stem.removeprefix("test_")
    words = name.split("_")
    prefixes = {"_".join(words[:size]) for size in range(1, len(words) + 1)}

    assert prefixes & MODULES or name in CROSS_CUTTING_SUITES or words[0] == "worker"


@pytest.mark.parametrize(
    "path", sorted((ROOT / "evals").glob("test_*.py")), ids=lambda path: path.name
)
def test_eval_files_end_with_eval(path: Path) -> None:
    assert path.stem.endswith("_eval")


def _public_functions() -> list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    return [
        (f"{path.name}:{node.name}", node)
        for path in SOURCE_FILES
        for node in ast.parse(path.read_text()).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]


PUBLIC_FUNCTIONS = _public_functions()


@pytest.mark.parametrize("function", PUBLIC_FUNCTIONS, ids=lambda item: item[0])
def test_public_functions_use_the_factory_verb_table(function) -> None:
    _name, node = function

    assert not node.name.startswith(FORBIDDEN_FACTORY_PREFIXES)
    if node.name.startswith("create_"):
        assert isinstance(node, ast.FunctionDef)
    if node.name.startswith("open_"):
        decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
        assert decorators & {"asynccontextmanager", "contextmanager"}
