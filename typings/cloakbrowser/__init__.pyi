import os
from typing import Any, Literal

from playwright.async_api import BrowserContext

async def launch_persistent_context_async(
    user_data_dir: str | os.PathLike[str],
    headless: bool = True,
    proxy: Any = None,
    args: list[str] | None = None,
    stealth_args: bool = True,
    user_agent: str | None = None,
    viewport: Any = ...,
    locale: str | None = None,
    timezone: str | None = None,
    color_scheme: Literal["light", "dark", "no-preference"] | None = None,
    geoip: bool = False,
    humanize: bool = False,
    human_preset: Literal["default", "careful"] = "default",
    human_config: Any = None,
    extension_paths: list[str] | None = None,
    license_key: str | None = None,
    browser_version: str | None = None,
    release_channel: str | None = None,
    **kwargs: Any,
) -> BrowserContext: ...
def ensure_binary(
    license_key: str | None = None,
    browser_version: str | None = None,
    release_channel: str | None = None,
) -> str: ...
