from pathlib import Path

from pydantic import TypeAdapter

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_json, emit_text
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.debrief.debrief import debrief_report_status
from fakeyou.foundation.invariants import SessionId
from fakeyou.meet.browser import BrowserLaunch, open_browser_tools
from fakeyou.observability.journal import journal_path
from fakeyou.observability.report import build_session_report, read_journal
from fakeyou.persistence.record import meeting_record_path


def check_setup(settings: Settings) -> None:
    emit_json(settings.model_dump(mode="json"))


async def show_browser_tools(settings: Settings) -> None:
    async with open_browser_tools(
        launch=BrowserLaunch(
            headless=settings.browser.headless,
            license_key=settings.browser.license_key.get_secret_value()
            if settings.browser.license_key
            else None,
        )
    ) as tools:
        emit_text("\n".join(tool.name for tool in tools))


def show_session_report(meeting_id: str, journal_dir: Path | None, settings: Settings) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    path = journal_path(journal_dir or settings.paths.journal_dir, meeting_id)
    if not path.exists():
        raise FakeYouError(ErrorCode.DIAGNOSTICS_JOURNAL_MISSING, meeting_id=meeting_id, path=path)
    report = build_session_report(read_journal(path))
    report["debrief"] = {
        "status": debrief_report_status(meeting_record_path(settings.paths.record_dir, meeting_id))
    }
    emit_json({"meeting_id": meeting_id, **report})
