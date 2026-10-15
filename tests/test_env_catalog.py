from pathlib import Path

from fakeyou.config.env_catalog import render_env_catalog

PAGE = Path(__file__).resolve().parent.parent / "docs" / "reference" / "environment.md"


def test_env_catalog_page_is_generated_from_the_catalog() -> None:
    assert PAGE.read_text(encoding="utf-8") == render_env_catalog()


def test_union_discriminator_is_one_row_listing_every_provider() -> None:
    rendered = render_env_catalog()
    provider_rows = [line for line in rendered.splitlines() if "FAKEYOU_TTS__PROVIDER" in line]

    assert len(provider_rows) == 1
    for provider in ("openai", "minimax", "cartesia", "elevenlabs"):
        assert f"'{provider}'" in provider_rows[0]
