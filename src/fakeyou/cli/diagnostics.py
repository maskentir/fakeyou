import json
from pathlib import Path

from pydantic import TypeAdapter

from fakeyou.browser import open_browser_tools
from fakeyou.cli.errors import CliError
from fakeyou.debrief import debrief_report_status
from fakeyou.invariants import SessionId
from fakeyou.journal import journal_path
from fakeyou.record import meeting_record_path
from fakeyou.report import build_session_report, read_journal
from fakeyou.settings import (
    Settings,
)


def check_setup(settings: Settings) -> None:
    print(json.dumps(settings.model_dump(mode="json"), indent=2))


async def show_browser_tools(settings: Settings) -> None:
    async with open_browser_tools(
        headless=settings.browser.headless,
        license_key=settings.browser.license_key.get_secret_value()
        if settings.browser.license_key
        else None,
    ) as tools:
        print("\n".join(tool.name for tool in tools))


def show_session_report(meeting_id: str, journal_dir: Path | None, settings: Settings) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    path = journal_path(journal_dir or settings.paths.journal_dir, meeting_id)
    if not path.exists():
        raise CliError(f"no journal for meeting {meeting_id} at {path}")
    report = build_session_report(read_journal(path))
    report["debrief"] = {
        "status": debrief_report_status(meeting_record_path(settings.paths.record_dir, meeting_id))
    }
    print(json.dumps({"meeting_id": meeting_id, **report}, ensure_ascii=False, indent=2))
