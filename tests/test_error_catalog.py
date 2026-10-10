from pathlib import Path

from fakeyou.catalog.error_catalog import render_error_catalog

PAGE = Path(__file__).resolve().parent.parent / "docs" / "reference" / "error-catalog.md"


def test_error_catalog_page_is_generated_from_the_catalog() -> None:
    assert PAGE.read_text(encoding="utf-8") == render_error_catalog()
