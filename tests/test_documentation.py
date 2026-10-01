import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCUMENTS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]
LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)")
DOC_CLASSES = {"plans", "reports", "reference", "runbooks"}


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda path: str(path.relative_to(ROOT)))
def test_relative_links_resolve(document: Path) -> None:
    targets = [
        target for target in LINK.findall(document.read_text()) if not re.match(r"^[a-z]+:", target)
    ]

    assert [target for target in targets if not (document.parent / target).exists()] == []


@pytest.mark.parametrize(
    "document",
    sorted((ROOT / "docs").rglob("*.md")),
    ids=lambda path: str(path.relative_to(ROOT)),
)
def test_documents_follow_the_folder_taxonomy(document: Path) -> None:
    relative = document.relative_to(ROOT / "docs")

    assert len(relative.parts) == 2
    assert relative.parts[0] in DOC_CLASSES
    if relative.parts[0] == "reports":
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-[a-z0-9-]+\.md", relative.name)
    else:
        assert re.fullmatch(r"[a-z0-9-]+\.md", relative.name)
