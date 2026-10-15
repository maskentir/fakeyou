from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_text
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.diarization.diarize_service import diarize_status


def _format_turn_count(count: int | None) -> str:
    return f"turns: {count if count is not None else 'unreadable'}"


def diarize_show(meeting_id: SessionId, *, settings: Settings) -> ExitCode:
    status = diarize_status(meeting_id, settings=settings)
    emit_text(_format_turn_count(status.turn_count))
    emit_text(f"covered: {status.covered}/{status.total} transcript entries")
    if status.stale_reason is not None:
        emit_text(f"status: stale: {status.stale_reason}")
        raise SystemExit(ExitCode.INPUT_REJECTED) from None
    emit_text("status: ok")
    return ExitCode.SUCCESS
