from pathlib import Path

from fakeyou.config.env_catalog import render_env_catalog

PAGE = Path(__file__).resolve().parent.parent / "docs" / "reference" / "environment.md"


def test_env_catalog_page_is_generated_from_the_catalog() -> None:
    assert PAGE.read_text(encoding="utf-8") == render_env_catalog()
