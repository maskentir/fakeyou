import asyncio
import functools
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn
from uuid import UUID

import typer
from pydantic import AwareDatetime, TypeAdapter, ValidationError

from fakeyou.catalog.errors import ErrorCode, FakeYouError, render_error
from fakeyou.cli.audio import audio_purge_expired, visual_purge_expired
from fakeyou.cli.calibrate import calibrate_eval, calibrate_lock
from fakeyou.cli.chat import chat
from fakeyou.cli.db import db_stamp, db_status, db_upgrade
from fakeyou.cli.debrief import debrief_meeting
from fakeyou.cli.diagnostics import check_setup, show_browser_tools, show_session_report
from fakeyou.cli.diarize import diarize_show
from fakeyou.cli.join import JoinOptions, join_meeting
from fakeyou.cli.memory import (
    CorrectionTarget,
    add_owner_note,
    correct_owner_note,
    forget_memory,
    ingest_recorded_meeting,
    initialize_memory,
    reembed_memory,
)
from fakeyou.cli.output import emit_error
from fakeyou.cli.voiceprint import (
    voiceprint_challenge,
    voiceprint_delete,
    voiceprint_enroll,
    voiceprint_list,
    voiceprint_purge_expired,
    voiceprint_verify,
)
from fakeyou.cli.worker import worker_ingest, worker_start, worker_status, worker_stop
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.db.checkpoint import is_database_error
from fakeyou.diarization.diarize_service import diarize_run
from fakeyou.identity.enrollment import EnrollSubject, LivenessGate
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.worker.worker_backend import WorkerStartRequest

_SOURCE_AT_ARGUMENT = TypeAdapter(AwareDatetime)


def _source_at_value(value: str) -> datetime:
    try:
        return _SOURCE_AT_ARGUMENT.validate_python(value)
    except ValidationError:
        raise typer.BadParameter(
            render_error(ErrorCode.MEMORY_SOURCE_AT_INVALID, value=value)
        ) from None


def _note_id_value(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise typer.BadParameter(render_error(ErrorCode.NOTE_ID_NOT_UUID, value=value)) from None


def _load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as error:
        raise FakeYouError(ErrorCode.CONFIG_INVALID, reason=_describe(error)) from None


def _guard_interrupts(command: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(command)
    def run(*args: Any, **kwargs: Any) -> None:
        try:
            command(*args, **kwargs)
        except KeyboardInterrupt:
            raise FakeYouError(ErrorCode.OPERATION_INTERRUPTED) from None

    return run


def _operator_identity(
    persona_path: Path | None, owner_id: str | None, twin_id: str | None
) -> OperatorIdentity:
    return OperatorIdentity(persona_path, owner_id, twin_id)


app = typer.Typer(
    help="Digital twin integration foundation",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
worker_app = typer.Typer(
    help="Start, monitor, and stop meeting workers",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
db_app = typer.Typer(
    help="Manage the database schema",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
voiceprint_app = typer.Typer(
    help="Enroll and verify speaker voiceprints",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
calibrate_app = typer.Typer(
    help="Lock and evaluate verified-name calibration",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
audio_app = typer.Typer(
    help="Manage persisted meeting audio",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
visual_app = typer.Typer(
    help="Manage persisted meeting visual evidence",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
diarize_app = typer.Typer(
    help="Run offline speaker correction for one meeting",
    add_completion=False,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)
app.add_typer(worker_app, name="worker")
app.add_typer(db_app, name="db")
app.add_typer(voiceprint_app, name="voiceprint")
app.add_typer(calibrate_app, name="calibrate")
app.add_typer(audio_app, name="audio")
app.add_typer(visual_app, name="visual")
app.add_typer(diarize_app, name="diarize")


@app.command("check", help="Inspect installed packages and credential presence")
@_guard_interrupts
def check_command() -> None:
    check_setup(settings=_load_settings())


@app.command("browser-tools", help="Launch CloakBrowser and list its native MCP tools")
@_guard_interrupts
def browser_tools_command() -> None:
    asyncio.run(show_browser_tools(settings=_load_settings()))


@app.command("memory-init", help="Create the official Graphiti database indexes")
@_guard_interrupts
def memory_init_command() -> None:
    asyncio.run(initialize_memory(settings=_load_settings()))


@app.command("memory-add", help="Store an owner note in the persona's memory")
@_guard_interrupts
def memory_add_command(
    text: Annotated[str, typer.Argument()],
    *,
    source_at: Annotated[str | None, typer.Option("--source-at")] = None,
    note_id: Annotated[str | None, typer.Option("--note-id")] = None,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    asyncio.run(
        add_owner_note(
            text=text,
            identity=_operator_identity(persona_path, owner_id, twin_id),
            settings=_load_settings(),
            source_at=_source_at_value(source_at) if source_at is not None else None,
            note_id=_note_id_value(note_id) if note_id is not None else None,
        )
    )


@app.command(
    "memory-correct", help="Replace one owner note with a corrected note in the persona's memory"
)
@_guard_interrupts
def memory_correct_command(
    text: Annotated[str, typer.Argument()],
    *,
    episode_uuid: Annotated[str, typer.Option("--episode")],
    source_at: Annotated[str, typer.Option("--source-at")],
    note_id: Annotated[str | None, typer.Option("--note-id")] = None,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    asyncio.run(
        correct_owner_note(
            text=text,
            target=CorrectionTarget(
                episode_uuid, _note_id_value(note_id) if note_id is not None else None
            ),
            source_at=_source_at_value(source_at),
            identity=_operator_identity(persona_path, owner_id, twin_id),
            settings=_load_settings(),
        )
    )


@app.command("memory-forget", help="Delete one meeting or one episode from the persona's memory")
@_guard_interrupts
def memory_forget_command(
    *,
    meeting_id: Annotated[str | None, typer.Option("--meeting-id")] = None,
    episode_uuid: Annotated[str | None, typer.Option("--episode")] = None,
    keep_record: Annotated[
        bool,
        typer.Option(
            "--keep-record",
            help="Keep the meeting record, journal, and thread "
            "so the meeting can be ingested again",
        ),
    ] = False,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    asyncio.run(
        forget_memory(
            identity=_operator_identity(persona_path, owner_id, twin_id),
            meeting_id=parse_session_id(meeting_id, field="--meeting-id")
            if meeting_id is not None
            else None,
            episode_uuid=episode_uuid,
            settings=_load_settings(),
            keep_record=keep_record,
        )
    )


@app.command("memory-ingest", help="Ingest a recorded meeting into the persona's memory, resuming")
@_guard_interrupts
def memory_ingest_command(
    *,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
    twin_name: Annotated[str | None, typer.Option("--twin-name")] = None,
    no_correct: Annotated[
        bool,
        typer.Option(
            "--no-correct",
            help="Skip automatic diarization before ingesting the live transcript",
        ),
    ] = False,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    asyncio.run(
        ingest_recorded_meeting(
            meeting_id=parse_session_id(meeting_id, field="--meeting-id"),
            identity=_operator_identity(persona_path, owner_id, twin_id),
            twin_name=twin_name,
            settings=_load_settings(),
            no_correct=no_correct,
        )
    )


@app.command(
    "memory-reembed",
    help="Recompute the persona's entity and fact embeddings with the configured model",
)
@_guard_interrupts
def memory_reembed_command(
    *,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    asyncio.run(
        reembed_memory(
            identity=_operator_identity(persona_path, owner_id, twin_id),
            settings=_load_settings(),
        )
    )


@app.command(
    "report", help="Summarize latency, replies, tools, ingestion, and faults of one meeting"
)
@_guard_interrupts
def report_command(
    *,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
    journal_dir: Annotated[Path | None, typer.Option("--journal-dir")] = None,
) -> None:
    show_session_report(
        meeting_id=parse_session_id(meeting_id, field="--meeting-id"),
        journal_dir=journal_dir,
        settings=_load_settings(),
    )


@app.command("debrief", help="Generate a source-backed meeting debrief for operator review")
@_guard_interrupts
def debrief_command(
    *,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    asyncio.run(
        debrief_meeting(
            meeting_id=parse_session_id(meeting_id, field="--meeting-id"),
            identity=_operator_identity(persona_path, owner_id, twin_id),
            force=force,
            settings=_load_settings(),
        )
    )


@app.command("join", help="Join a Google Meet link as a guest twin")
@_guard_interrupts
def join_command(
    link: Annotated[str, typer.Argument()],
    *,
    twin_name: Annotated[str | None, typer.Option("--twin-name")] = None,
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
    meeting_id: Annotated[str | None, typer.Option("--meeting-id")] = None,
    lobby_timeout: Annotated[float | None, typer.Option("--lobby-timeout")] = None,
    duration: Annotated[float | None, typer.Option("--duration")] = None,
    profile_dir: Annotated[str | None, typer.Option("--profile-dir")] = None,
    headless: Annotated[bool | None, typer.Option("--headless")] = None,
    audio_enabled: Annotated[
        bool, typer.Option("--audio", help="Enable Linux virtual audio and Muse transcription")
    ] = False,
    initial_speech: Annotated[
        str | None,
        typer.Option(
            "--speak", help="Speak this test sentence once after admission; requires --audio"
        ),
    ] = None,
    conversation_enabled: Annotated[
        bool,
        typer.Option(
            "--converse", help="Enable agent replies to final transcripts; requires --audio"
        ),
    ] = False,
    persona_path: Annotated[
        Path | None,
        typer.Option("--persona", help="Owner persona JSON profile; requires --converse"),
    ] = None,
    brief_path: Annotated[
        Path | None,
        typer.Option(
            "--brief",
            help="Per-meeting brief JSON; requires --converse, --persona, and --meeting-id",
        ),
    ] = None,
    memory: Annotated[
        bool,
        typer.Option(
            "--memory",
            help="Enable persona-scoped Graphiti recall; requires --converse and --persona",
        ),
    ] = False,
    wake_phrase: Annotated[
        str | None,
        typer.Option(
            "--wake-phrase",
            help="Comma-separated phrases that address the twin; requires --converse",
        ),
    ] = None,
    barge_in: Annotated[
        bool,
        typer.Option("--barge-in", help="Let new speech interrupt playback; requires --converse"),
    ] = False,
    resume: Annotated[
        bool, typer.Option("--resume", help="Resume an unfinished session for the same meeting ID")
    ] = False,
) -> None:
    asyncio.run(
        join_meeting(
            link=link,
            options=JoinOptions(
                identity=_operator_identity(persona_path, owner_id, twin_id),
                twin_name=twin_name,
                meeting_id=meeting_id,
                lobby_timeout=lobby_timeout,
                duration=duration,
                profile_dir=profile_dir,
                audio_enabled=audio_enabled,
                initial_speech=initial_speech,
                headless=headless,
                memory=memory,
                conversation_enabled=conversation_enabled,
                wake_phrase=wake_phrase,
                barge_in=barge_in,
                brief_path=brief_path,
                resume=resume,
            ),
            settings=_load_settings(),
        )
    )


@app.command("chat", help="Run one agent turn with a persistent checkpoint")
@_guard_interrupts
def chat_command(
    message: Annotated[str, typer.Argument()],
    *,
    thread: Annotated[str, typer.Option("--thread")],
    use_browser: Annotated[
        bool, typer.Option("--browser", help="Enable real browser actions")
    ] = False,
    use_memory: Annotated[
        bool, typer.Option("--memory", help="Enable Graphiti recall and writes")
    ] = False,
) -> None:
    asyncio.run(
        chat(
            message=message,
            thread=thread,
            use_browser=use_browser,
            use_memory=use_memory,
            settings=_load_settings(),
        )
    )


@worker_app.command("start", help="Start a meeting worker in the background")
@_guard_interrupts
def worker_start_command(
    link: Annotated[str, typer.Argument()],
    *,
    twin_name: Annotated[str | None, typer.Option("--twin-name")] = None,
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
    lobby_timeout: Annotated[float | None, typer.Option("--lobby-timeout")] = None,
    duration: Annotated[float | None, typer.Option("--duration")] = None,
    profile_dir: Annotated[str | None, typer.Option("--profile-dir")] = None,
    headless: Annotated[bool | None, typer.Option("--headless")] = None,
    audio_enabled: Annotated[bool, typer.Option("--audio")] = False,
    initial_speech: Annotated[str | None, typer.Option("--speak")] = None,
    conversation_enabled: Annotated[bool, typer.Option("--converse")] = False,
    persona_path: Annotated[Path | None, typer.Option("--persona")] = None,
    brief_path: Annotated[Path | None, typer.Option("--brief")] = None,
    memory: Annotated[bool, typer.Option("--memory")] = False,
    wake_phrase: Annotated[str | None, typer.Option("--wake-phrase")] = None,
    barge_in: Annotated[bool, typer.Option("--barge-in")] = False,
) -> None:
    identity = _operator_identity(persona_path, owner_id, twin_id)
    worker_start(
        request=WorkerStartRequest(
            link=link,
            meeting_id=meeting_id,
            owner_id=identity.owner_id,
            twin_id=identity.twin_id,
            twin_name=twin_name,
            lobby_timeout=lobby_timeout,
            duration=duration,
            profile_dir=profile_dir,
            headless=headless,
            audio=audio_enabled,
            initial_speech=initial_speech,
            conversation=conversation_enabled,
            persona_path=identity.persona_path,
            brief_path=brief_path,
            memory=memory,
            wake_phrase=wake_phrase,
            barge_in=barge_in,
        ),
        settings=_load_settings(),
    )


@worker_app.command("status", help="Show worker status for one meeting or all")
@_guard_interrupts
def worker_status_command(
    meeting_id: Annotated[str | None, typer.Argument()] = None,
) -> None:
    worker_status(meeting_id=meeting_id, settings=_load_settings())


@worker_app.command("stop", help="Stop a meeting worker gracefully")
@_guard_interrupts
def worker_stop_command(
    meeting_id: Annotated[str, typer.Argument()],
    *,
    timeout: Annotated[float, typer.Option("--timeout")] = 60.0,
) -> None:
    worker_stop(meeting_id=meeting_id, timeout=timeout, settings=_load_settings())


@worker_app.command("ingest", help="Ingest a finished meeting in a separate container")
@_guard_interrupts
def worker_ingest_command(
    meeting_id: Annotated[str, typer.Argument()],
    *,
    persona_path: Annotated[Path, typer.Option("--persona")],
    owner_id: Annotated[str | None, typer.Option("--owner-id")] = None,
    twin_id: Annotated[str | None, typer.Option("--twin-id")] = None,
) -> None:
    worker_ingest(
        meeting_id=meeting_id,
        identity=_operator_identity(persona_path, owner_id, twin_id),
        settings=_load_settings(),
    )


@db_app.command("upgrade", help="Apply schema migrations up to head")
@_guard_interrupts
def db_upgrade_command() -> None:
    db_upgrade(settings=_load_settings())


@db_app.command("stamp", help="Adopt an existing voiceprints table as the baseline revision")
@_guard_interrupts
def db_stamp_command() -> None:
    db_stamp(settings=_load_settings())


@db_app.command("status", help="Show the applied schema revision and head")
@_guard_interrupts
def db_status_command() -> None:
    db_status(settings=_load_settings())


@voiceprint_app.command("enroll", help="Enroll a voiceprint from audio clips")
@_guard_interrupts
def voiceprint_enroll_command(
    *,
    name: Annotated[str, typer.Option("--name")],
    clip: Annotated[list[str], typer.Option("--clip")],
    negative: Annotated[bool, typer.Option("--negative")] = False,
    challenge_id: Annotated[str | None, typer.Option("--challenge-id")] = None,
    challenge: Annotated[str | None, typer.Option("--challenge")] = None,
    transcript: Annotated[str | None, typer.Option("--transcript")] = None,
    owner_name: Annotated[str | None, typer.Option("--owner-name")] = None,
    spontaneous_clip: Annotated[list[str] | None, typer.Option("--spontaneous-clip")] = None,
) -> None:
    voiceprint_enroll(
        subject=EnrollSubject(name, negative),
        clip=clip,
        gate=LivenessGate(
            challenge_id,
            challenge,
            transcript,
            owner_name,
            tuple(spontaneous_clip or ()),
        ),
        settings=_load_settings(),
    )


@voiceprint_app.command("challenge", help="Issue an enrollment liveness challenge")
@_guard_interrupts
def voiceprint_challenge_command() -> None:
    voiceprint_challenge()


@voiceprint_app.command("verify", help="Verify an audio clip against a voiceprint")
@_guard_interrupts
def voiceprint_verify_command(
    *,
    name: Annotated[str, typer.Option("--name")],
    clip: Annotated[str, typer.Option("--clip")],
) -> None:
    voiceprint_verify(name=name, clip=clip, settings=_load_settings())


@voiceprint_app.command("delete", help="Delete one enrolled voiceprint")
@_guard_interrupts
def voiceprint_delete_command(
    *,
    name: Annotated[str, typer.Option("--name")],
) -> None:
    voiceprint_delete(name=name, settings=_load_settings())


@voiceprint_app.command("list", help="List enrolled voiceprints")
@_guard_interrupts
def voiceprint_list_command() -> None:
    voiceprint_list(settings=_load_settings())


@voiceprint_app.command("purge-expired", help="Delete voiceprints past retention")
@_guard_interrupts
def voiceprint_purge_expired_command() -> None:
    voiceprint_purge_expired(settings=_load_settings())


@calibrate_app.command("lock", help="Fit calibration from dev score journals")
@_guard_interrupts
def calibrate_lock_command(
    *,
    dev_journals: Annotated[list[str], typer.Option("--dev")],
    out: Annotated[str, typer.Option("--out")],
    model_version: Annotated[str | None, typer.Option("--model-version")] = None,
) -> None:
    calibrate_lock(dev_journals=dev_journals, out=out, model_version=model_version)


@calibrate_app.command("eval", help="Evaluate calibration on eval score journals")
@_guard_interrupts
def calibrate_eval_command(
    *,
    eval_journals: Annotated[list[str], typer.Option("--eval")],
    artifact: Annotated[str, typer.Option("--artifact")],
    model_version: Annotated[str | None, typer.Option("--model-version")] = None,
) -> None:
    calibrate_eval(
        eval_journals=eval_journals,
        artifact=artifact,
        model_version=model_version,
    )


@audio_app.command("purge-expired", help="Delete meeting audio past retention")
@_guard_interrupts
def audio_purge_expired_command(
    *,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    days: Annotated[int | None, typer.Option("--days")] = None,
) -> None:
    audio_purge_expired(root=root, days=days, settings=_load_settings())


@visual_app.command("purge-expired", help="Delete meeting visual evidence past retention")
@_guard_interrupts
def visual_purge_expired_command(
    *,
    root: Annotated[Path | None, typer.Option("--root")] = None,
    days: Annotated[int | None, typer.Option("--days")] = None,
) -> None:
    visual_purge_expired(root=root, days=days, settings=_load_settings())


@diarize_app.command("run", help="Mix slot audio and write diarization sidecars")
@_guard_interrupts
def diarize_run_command(
    *,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
) -> None:
    diarize_run(
        meeting_id=parse_session_id(meeting_id, field="--meeting-id"), settings=_load_settings()
    )


@diarize_app.command("show", help="Show diarization correction coverage")
@_guard_interrupts
def diarize_show_command(
    *,
    meeting_id: Annotated[str, typer.Option("--meeting-id")],
) -> None:
    diarize_show(
        meeting_id=parse_session_id(meeting_id, field="--meeting-id"), settings=_load_settings()
    )


def main() -> None:
    try:
        app()
    except (asyncio.CancelledError, KeyboardInterrupt):
        _report(FakeYouError(ErrorCode.OPERATION_INTERRUPTED))
    except FakeYouError as error:
        _report(error)
    except ValidationError as error:
        _report(FakeYouError(ErrorCode.INPUT_INVALID, reason=_describe(error)))
    except ProviderFault as fault:
        _report(FakeYouError(ErrorCode.PROVIDER_UNAVAILABLE, reason=fault))
    except Exception as error:
        if not is_database_error(error):
            raise
        _report(FakeYouError(ErrorCode.DATABASE_UNREACHABLE))


def _report(error: FakeYouError) -> NoReturn:
    emit_error(error)
    raise SystemExit(error.exit_code) from None


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc']) or 'input'}: {issue['msg']}"
        for issue in error.errors(include_url=False, include_input=False)
    )
