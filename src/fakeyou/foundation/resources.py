from importlib.resources import files
from importlib.resources.abc import Traversable

PACKAGE_NAME = "fakeyou"


def package_resource(*parts: str) -> Traversable:
    return files(PACKAGE_NAME).joinpath(*parts)


def read_package_text(*parts: str) -> str:
    return package_resource(*parts).read_text(encoding="utf-8")


RTC_BRIDGE_ASSET = "rtc_bridge.js"


def load_bridged_script(name: str) -> str:
    return f"{read_package_text('assets', RTC_BRIDGE_ASSET)}\n{read_package_text('assets', name)}"
