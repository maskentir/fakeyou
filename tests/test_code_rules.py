import ast
import io
import json
import re
import string
import tokenize
from pathlib import Path

import pytest

from fakeyou.catalog.errors import CATALOG, ErrorCode

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
DATABASE_PACKAGE = ROOT / "src" / "fakeyou" / "db"
PERSISTENCE_DRIVERS = ("alembic", "langgraph.checkpoint.postgres", "psycopg", "sqlalchemy")


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


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _is_persistence_driver(module: str) -> bool:
    return any(
        module == driver or module.startswith(f"{driver}.") for driver in PERSISTENCE_DRIVERS
    )


@pytest.mark.parametrize(
    "path",
    [path for path in SOURCE_FILES if DATABASE_PACKAGE not in path.parents],
    ids=_relative,
)
def test_persistence_drivers_are_imported_only_by_the_db_package(path: Path) -> None:
    drivers = _imported_modules(path.read_text())

    assert sorted(module for module in drivers if _is_persistence_driver(module)) == []


ERROR_CATALOG_FILE = ROOT / "src" / "fakeyou" / "catalog" / "errors.py"
OUTPUT_FILE = ROOT / "src" / "fakeyou" / "cli" / "output.py"
JOURNAL_FILE = ROOT / "src" / "fakeyou" / "observability" / "journal.py"
RETIRED_ERROR_NAMES = ("CliError", "PersistenceError", "FAILURE_EXIT_CODES")


def _error_classes() -> frozenset[str]:
    parents: dict[str, set[str]] = {}
    for path in SOURCE_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                parents.setdefault(node.name, set()).update(
                    ast.unparse(base) for base in node.bases
                )
    classes = {"FakeYouError"}
    grew = True
    while grew:
        grew = False
        for name, bases in parents.items():
            if name not in classes and bases & classes:
                classes.add(name)
                grew = True
    return frozenset(classes)


def _template_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name is not None}


ERROR_CLASSES = _error_classes()
ERROR_CONSTRUCTIONS = [
    (_relative(path), node)
    for path in SOURCE_FILES
    if path != ERROR_CATALOG_FILE
    for node in ast.walk(ast.parse(path.read_text()))
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id in ERROR_CLASSES
]


@pytest.mark.parametrize(
    "site", ERROR_CONSTRUCTIONS, ids=lambda site: f"{site[0]}:{site[1].lineno}"
)
def test_error_construction_names_a_catalog_code_and_its_fields(
    site: tuple[str, ast.Call],
) -> None:
    _location, call = site
    first = call.args[0] if call.args else None

    assert isinstance(first, ast.Attribute)
    assert ast.unparse(first.value) == "ErrorCode"
    code = ErrorCode[first.attr]
    keywords = {keyword.arg for keyword in call.keywords}
    assert None not in keywords
    assert keywords == _template_fields(CATALOG[code].template)


def test_every_error_code_is_constructed_somewhere_in_the_source() -> None:
    used = {
        node.attr
        for path in SOURCE_FILES
        if path != ERROR_CATALOG_FILE
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and ast.unparse(node.value) == "ErrorCode"
    }

    assert used == {member.name for member in ErrorCode}


def test_user_facing_failure_text_is_printed_only_by_the_output_module() -> None:
    offenders = [
        _relative(path)
        for path in SOURCE_FILES
        if path != OUTPUT_FILE and "fakeyou: error:" in path.read_text()
    ]

    assert offenders == []


def test_print_calls_live_only_in_the_output_module_and_the_journal() -> None:
    offenders = [
        f"{_relative(path)}:{node.lineno}"
        for path in SOURCE_FILES
        if path not in {OUTPUT_FILE, JOURNAL_FILE}
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
    ]

    assert offenders == []


def test_system_exit_never_takes_a_bare_integer() -> None:
    offenders = [
        f"{_relative(path)}:{node.lineno}"
        for path in SOURCE_FILES
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "SystemExit"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]

    assert offenders == []


@pytest.mark.parametrize("name", RETIRED_ERROR_NAMES)
def test_retired_error_names_are_gone(name: str) -> None:
    pattern = re.compile(rf"\b{name}\b")
    offenders = [
        _relative(path)
        for folder in ("src", "tests", "evals")
        for path in (ROOT / folder).rglob("*.py")
        if path.resolve() != Path(__file__).resolve() and pattern.search(path.read_text())
    ]

    assert offenders == []
