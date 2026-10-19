from pathlib import Path

import pytest

from fakeyou.foundation.resources import package_resource, read_package_text


def test_reads_a_bundled_json_file_from_the_package_root() -> None:
    assert read_package_text("number_vocabulary.json").lstrip().startswith("{")


def test_reads_a_nested_asset() -> None:
    assert package_resource("assets", "stream_capture.js").is_file()


def test_reads_a_resource_from_a_subpackage() -> None:
    assert package_resource("catalog", "error_catalog.json").is_file()


def test_resource_handle_is_the_bundled_file_on_disk() -> None:
    expected = Path(__file__).resolve().parent.parent / "src" / "fakeyou" / "number_vocabulary.json"

    assert Path(str(package_resource("number_vocabulary.json"))) == expected


def test_missing_resource_is_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        read_package_text("missing-resource.json")
