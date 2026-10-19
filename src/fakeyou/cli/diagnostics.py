import httpx2
from playwright.async_api import Error as PlaywrightError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.client import ApiClient, ClientSettings, admin_client, owner_client
from fakeyou.cli.output import emit_json, emit_text
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.meet.browser import BrowserLaunch, open_browser_tools


async def check_api(
    settings: ClientSettings, *, transport: httpx2.AsyncBaseTransport | None = None
) -> None:
    async with ApiClient(base_url=settings.base_url, token=None, transport=transport) as anonymous:
        emit_json({"api": settings.base_url, "health": await anonymous.health()})
    if settings.admin_token is None:
        return
    async with admin_client(settings, transport=transport) as admin:
        emit_json(await admin.admin_check())


async def show_browser_tools(settings: Settings) -> None:
    try:
        async with open_browser_tools(
            launch=BrowserLaunch(
                headless=settings.browser.headless,
                license_key=settings.browser.license_key.get_secret_value()
                if settings.browser.license_key
                else None,
            ),
            output_dir=settings.paths.browser_output_dir,
        ) as tools:
            emit_text("\n".join(tool.name for tool in tools))
    except (PlaywrightError, OSError) as error:
        raise FakeYouError(ErrorCode.BROWSER_TOOLS_FAILED, reason=type(error).__name__) from error


async def show_session_report(
    *, meeting_id: str, settings: ClientSettings, transport: httpx2.AsyncBaseTransport | None = None
) -> None:
    async with owner_client(settings, transport=transport) as client:
        emit_json(await client.get_report(meeting_id))
