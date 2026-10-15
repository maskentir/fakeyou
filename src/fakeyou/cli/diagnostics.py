from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_json, emit_text
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.debrief.debrief import debrief_report_status
from fakeyou.meet.browser import BrowserLaunch, open_browser_tools
from fakeyou.observability.journal import journal_path
from fakeyou.observability.provider_faults import safe_endpoint
from fakeyou.observability.report import build_session_report, read_journal_with_corrupt
from fakeyou.persistence.record import meeting_record_path


def _masked_body(value: Any) -> Any:
    if isinstance(value, str):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: _masked_body(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_masked_body(item) for item in value]
    return value


def _masked_setting(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        if key == "extra_body":
            return {name: _masked_body(item) for name, item in value.items()}
        return {name: _masked_setting(name, item) for name, item in value.items()}
    if isinstance(value, list):
        return [_masked_setting(key, item) for item in value]
    if isinstance(value, str) and ("://" in value or key in ("base_url", "url")):
        return safe_endpoint(value)
    return value


def check_setup(settings: Settings) -> None:
    emit_json(_masked_setting("settings", settings.model_dump(mode="json")))


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


def show_session_report(
    meeting_id: SessionId, journal_dir: Path | None, settings: Settings
) -> None:
    path = journal_path(journal_dir or settings.paths.journal_dir, meeting_id)
    if not path.exists():
        raise FakeYouError(ErrorCode.DIAGNOSTICS_JOURNAL_MISSING, meeting_id=meeting_id, path=path)
    try:
        rows, corrupt = read_journal_with_corrupt(path)
        report = build_session_report(rows, corrupt_lines=corrupt)
        report["debrief"] = {
            "status": debrief_report_status(
                meeting_record_path(settings.paths.record_dir, meeting_id)
            )
        }
        emit_json({"meeting_id": meeting_id, **report})
    except OSError as error:
        raise FakeYouError(
            ErrorCode.DIAGNOSTICS_REPORT_FAILED, reason=type(error).__name__
        ) from error
