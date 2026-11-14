import ast
from pathlib import Path


def test_production_publication_wiring_never_imports_evaluation_adapters() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "fakeyou"
    imports: list[tuple[str, str]] = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports.extend((str(path.relative_to(root)), item.name) for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.append((str(path.relative_to(root)), node.module))
    assert [(path, module) for path, module in imports if module.split(".")[0] == "evals"] == []
