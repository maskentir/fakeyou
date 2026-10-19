import json
import sys
from typing import Any

import httpx2
import pytest

import fakeyou.cli.diagnostics as cli_diagnostics
import fakeyou.cli.main as cli_main
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.client import ClientSettings
from fakeyou.config.settings import Settings
from tests._error_support import message_pattern

SETTINGS = ClientSettings(base_url="http://api.test", owner_token="owner-secret", admin_token=None)


async def test_report_prints_the_api_document_for_the_meeting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    document = {"meeting_id": "meeting-1", "result": {"terminal_reason": "stopped_by_owner"}}
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx2.Response(200, json=document)

    await cli_diagnostics.show_session_report(
        meeting_id="meeting-1",
        settings=SETTINGS,
        transport=httpx2.MockTransport(handler),
    )

    assert seen == ["GET /v1/sessions/meeting-1/report"]
    assert json.loads(capsys.readouterr().out) == document


def test_report_command_rejects_an_invalid_meeting_id_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", "report", "--meeting-id", "bad meeting"])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 2
    assert "invalid --meeting-id: 'bad meeting'" in capsys.readouterr().err


async def test_browser_tools_command_maps_launch_errors_to_tools_failed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    from playwright.async_api import Error as PlaywrightError

    @asynccontextmanager
    async def failing(*args: Any, **_kwargs: Any) -> Any:
        del args
        raise PlaywrightError("launch blew up")
        yield []

    monkeypatch.setattr(cli_diagnostics, "open_browser_tools", failing)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.BROWSER_TOOLS_FAILED)
    ) as caught:
        await cli_diagnostics.show_browser_tools(settings)

    assert caught.value.code is ErrorCode.BROWSER_TOOLS_FAILED
    assert str(caught.value) == "browser tools failed: Error"
