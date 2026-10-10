from pathlib import Path

from pydantic import TypeAdapter

from fakeyou.browser import open_browser_tools
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.output import emit_json, emit_text
from fakeyou.debrief import debrief_report_status
from fakeyou.invariants import SessionId
from fakeyou.journal import journal_path
from fakeyou.record import meeting_record_path
from fakeyou.report import build_session_report, read_journal
from fakeyou.settings import (
    Settings,
)


def check_setup(settings: Settings) -> None:
    emit_json(settings.model_dump(mode="json"))


async def show_browser_tools(settings: Settings) -> None:
    async with open_browser_tools(
        headless=settings.browser.headless,
        license_key=settings.browser.license_key.get_secret_value()
        if settings.browser.license_key
        else None,
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
