from dataclasses import dataclass
from pathlib import Path

from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.orchestration.join import resume_command, run_join_session
from fakeyou.orchestration.join_types import JoinOutcome, JoinPorts, JoinRequest
from fakeyou.orchestration.providers import open_persona_memory
from fakeyou.orchestration.runtime import run_meeting_session


@dataclass(slots=True)
class JoinOptions:
    identity: OperatorIdentity
    twin_name: str | None = None
    meeting_id: str | None = None
    lobby_timeout: float | None = None
    duration: float | None = None
    profile_dir: str | None = None
    audio_enabled: bool = False
    initial_speech: str | None = None
    headless: bool | None = None
    memory: bool = False
    conversation_enabled: bool = False
    wake_phrase: str | None = None
    barge_in: bool = False
    brief_path: Path | None = None
    resume: bool = False


def _join_request(link: str, options: JoinOptions, settings: Settings) -> JoinRequest:
    return JoinRequest(
        link=link,
        settings=settings,
        identity=options.identity,
        audio_enabled=options.audio_enabled or settings.worker.audio,
        twin_name=options.twin_name,
        meeting_id=options.meeting_id,
        lobby_timeout=options.lobby_timeout,
        duration=options.duration,
        profile_dir=options.profile_dir,
        headless=options.headless,
        initial_speech=options.initial_speech,
        memory=options.memory,
        conversation_enabled=options.conversation_enabled,
        wake_phrase=options.wake_phrase,
        barge_in=options.barge_in,
        brief_path=options.brief_path,
        resume=options.resume,
    )


def _join_ports() -> JoinPorts:
    return JoinPorts(
        run_session=run_meeting_session,
        open_memory=open_persona_memory,
        open_checkpointer=open_checkpointer,
    )


def _emit_join_outcome(outcome: JoinOutcome) -> None:
    emit_json(
        {
            "meeting_id": outcome.meeting_id,
            "phase": outcome.session.phase,
            "terminal_reason": outcome.session.terminal_reason,
            "detail": outcome.session.detail,
            "error_code": outcome.error_code.value if outcome.error_code is not None else None,
            "faults": outcome.session.faults,
            "session_outcome": "completed" if outcome.session_completed else "failed",
            "ingestion_outcome": outcome.ingestion_outcome,
            "record_error": outcome.record_error,
            "resume_command": resume_command(outcome.ingest_resume),
            "brief_sha256": outcome.brief_sha256,
            "context_path": str(outcome.context_path) if outcome.context_path is not None else None,
        }
    )
    if outcome.exit_code != ExitCode.SUCCESS:
        raise SystemExit(outcome.exit_code)


async def join_meeting(link: str, *, options: JoinOptions, settings: Settings) -> None:
    outcome = await run_join_session(_join_request(link, options, settings), _join_ports())
    _emit_join_outcome(outcome)
