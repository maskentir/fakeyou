from importlib.resources import files
from importlib.resources.abc import Traversable

PACKAGE_NAME = "fakeyou"


def package_resource(*parts: str) -> Traversable:
    return files(PACKAGE_NAME).joinpath(*parts)


def read_package_text(*parts: str) -> str:
    return package_resource(*parts).read_text(encoding="utf-8")
