import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from graphiti_core import Graphiti
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.assembly import open_conversation, stop_signal_handlers
from fakeyou.audio import open_meeting_audio
from fakeyou.cli.errors import FAILURE_EXIT_CODES, CliError
from fakeyou.cli.memory import load_operator_persona
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    transcript_event,
)
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import meeting_windows
from fakeyou.ingestion_status import (
    IngestionStatus,
    RecordLockError,
    hash_meeting_record,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME
from fakeyou.journal import Journal, journal_path
from fakeyou.participant_observer import EVIDENCE_FILE_NAME
from fakeyou.persona import PersonaProfile, speech_keywords
from fakeyou.playback import PlaybackEvent
from fakeyou.provider_faults import ProviderFault, classify_exception
from fakeyou.providers import memory_config, open_persona_memory, preflight_hosts
from fakeyou.record import MeetingRecord, load_meeting_record, meeting_record_path
from fakeyou.runtime import run_meeting_session
from fakeyou.session import SessionConfig, SessionResult
from fakeyou.settings import (
    Settings,
)


async def join_meeting(
    link: str,
    *,
    twin_name: str | None,
    owner_id: str | None,
    twin_id: str | None,
    meeting_id: str | None,
    lobby_timeout: float | None,
    duration: float | None,
    profile_dir: str | None,
    settings: Settings,
    audio_enabled: bool = False,
    initial_speech: str | None = None,
    headless: bool | None = None,
    memory: bool = False,
    conversation_enabled: bool = False,
    wake_phrase: str | None = None,
    barge_in: bool = False,
    persona_path: Path | None = None,
) -> None:
    audio_enabled = audio_enabled or settings.worker.audio
    parsed_link = urlparse(link)
    if (
        parsed_link.scheme != "https"
        or parsed_link.hostname != "meet.google.com"
        or parsed_link.path.strip("/") == ""
    ):
        raise CliError("link must be an https://meet.google.com/ meeting URL")
    if lobby_timeout is not None and not lobby_timeout > 0:
        raise CliError("--lobby-timeout must be positive")
    if duration is not None and not duration > 0:
        raise CliError("--duration must be positive")
    if initial_speech is not None and (not audio_enabled or not initial_speech.strip()):
        raise CliError("--speak requires --audio and nonempty text")
    if conversation_enabled and not audio_enabled:
        raise CliError("--converse requires --audio")
    if memory and not conversation_enabled:
        raise CliError("--memory requires --converse")
    if persona_path is not None and not conversation_enabled:
        raise CliError("--persona requires --converse")
    if memory and persona_path is None:
        raise CliError("--memory requires --persona")
    if wake_phrase and not conversation_enabled:
        raise CliError("--wake-phrase requires --converse")
    if settings.conversation.participant_policy_enabled and not (wake_phrase or "").strip():
        raise CliError("participant policy requires --wake-phrase aliases")
    if barge_in and not conversation_enabled:
        raise CliError("--barge-in requires --converse")
    if conversation_enabled:
        settings.require_roles("reply", "reply_fallback")
    if memory:
        memory_config(settings)
    audio_factory = None
    if audio_enabled:
        if (
            sys.platform != "linux"
            or os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME
            or os.environ.get("PULSE_SINK") != AGENT_SINK_NAME
            or find_spec("pyaudio") is None
        ):
            raise CliError(
                "--audio requires the Linux audio worker; use docker compose run meeting-worker"
            )
        settings.require_roles("stt", "tts")
    stop = asyncio.Event()
    resolved_meeting_id = meeting_id or uuid.uuid4().hex[:12]
    resolved_twin_id = twin_id or settings.identity.twin_id
    resolved_headless = settings.browser.headless if headless is None else headless
    config = SessionConfig(
        owner_id=owner_id or settings.identity.owner_id,
        twin_id=resolved_twin_id,
        meeting_id=resolved_meeting_id,
        link=link,
        twin_name=twin_name or settings.identity.twin_name,
        browser_profile_dir=Path(profile_dir)
        if profile_dir
        else settings.paths.browser_profile_dir / resolved_twin_id,
        lobby_timeout_seconds=lobby_timeout
        if lobby_timeout is not None
        else settings.meeting.lobby_timeout_seconds,
        session_duration_seconds=duration
        if duration is not None
        else settings.meeting.session_duration_seconds,
        join_poll_interval_seconds=settings.meeting.join_poll_seconds,
        monitor_interval_seconds=settings.meeting.monitor_poll_seconds,
        leave_timeout_seconds=settings.meeting.leave_timeout_seconds,
        warmup_browser=settings.browser.warmup,
        deaf_window_seconds=settings.audio_health.deaf_window_seconds,
        deaf_grace_seconds=settings.audio_health.deaf_grace_seconds,
        preflight_budget_seconds=settings.preflight.budget_seconds,
        headless=resolved_headless,
        license_key=settings.browser.license_key.get_secret_value()
        if settings.browser.license_key
        else None,
        checkpoint_path=settings.paths.checkpoint_path,
    )
    profile: PersonaProfile | None = None
    if persona_path is not None:
        profile = await load_operator_persona(
            persona_path, config.owner_id, config.twin_id, settings
        )
        if profile.language != settings.speech.language:
            raise CliError("persona language must match speech.language")
        if memory and not (profile.allowed_tools or profile.ingest_transcripts):
            raise CliError(
                "--memory requires recall_meeting_context or ingest_transcripts in the persona"
            )
        if not twin_name:
            config = replace(config, twin_name=profile.display_name)
    journal = Journal(
        resolved_meeting_id, path=journal_path(settings.paths.journal_dir, resolved_meeting_id)
    )
    faults = FaultReporter(journal=journal)
    record_path = meeting_record_path(settings.paths.record_dir, resolved_meeting_id)
    config = replace(
        config,
        participant_policy_enabled=settings.conversation.participant_policy_enabled,
        participant_evidence_path=record_path.parent / EVIDENCE_FILE_NAME,
    )
    ingest_requested = profile is not None and profile.ingest_transcripts
    ingestion_outcome = "pending" if ingest_requested else "not_requested"
    record_error: str | None = None
    result = SessionResult("ended", None, "", None)
    exit_code = 1
    final_exit = 1
    try:
        with record_lock(record_path, purpose="meeting"):
            if profile is not None:
                try:
                    write_status(
                        status_path(record_path),
                        IngestionStatus(
                            meeting_id=resolved_meeting_id,
                            group_id=profile.memory_group,
                            status="pending" if ingest_requested else "not_requested",
                            updated_at=datetime.now(UTC),
                        ),
                    )
                except OSError as error:
                    faults.record(classify_exception("meeting_record", error))
                    record_error = f"ingestion status not written: {type(error).__name__}"
                    ingestion_outcome = "unknown"
            record = MeetingRecord(record_path, faults=faults)

            conversation: ConversationLoop | None = None
            graphiti: Graphiti | None = None
            session_result: SessionResult | None = None
            async with AsyncExitStack() as resources:
                resources.callback(journal.close)
                resources.callback(record.close)
                if memory and profile is not None:
                    try:
                        graphiti = await resources.enter_async_context(
                            open_persona_memory(settings)
                        )
                    except ProviderFault as fault:
                        faults.record(fault)
                        session_result = SessionResult(
                            "ended",
                            "infra_failure",
                            f"memory unavailable: {fault}",
                            faults.report(),
                        )

                def log_transition(
                    from_phase: ConversationPhase, to_phase: ConversationPhase, detail: str
                ) -> None:
                    journal.record(
                        "conversation_transitioned",
                        source="agent",
                        from_phase=from_phase,
                        to_phase=to_phase,
                        detail=detail,
                    )

                def log_playback(event: PlaybackEvent) -> None:
                    journal.record(
                        event.kind,
                        source="playback",
                        ends_with_question=event.text.rstrip().endswith("?"),
                        detail=event.detail,
                    )
                    if event.kind == "playback_finished":
                        record.add_twin_speech(event.text)

                if conversation_enabled and session_result is None:
                    conversation = await resources.enter_async_context(
                        open_conversation(
                            settings,
                            config,
                            graphiti=graphiti,
                            profile=profile,
                            on_tool_result=record.add_tool_result,
                            on_transition=log_transition,
                            wake_phrases=[
                                phrase.strip() for phrase in (wake_phrase or "").split(",")
                            ],
                            barge_in=barge_in,
                            faults=faults,
                            journal=journal,
                        )
                    )

                async def transcript(frame: TranscriptionFrame) -> None:
                    event = transcript_event(frame, "unknown", None)
                    journal.record(
                        "transcript_finalized",
                        source="stt",
                        origin=event.source,
                        transcript_id=event.id,
                        participant_id=event.participant_id,
                        speaker_evidence_id=event.speaker_evidence_id,
                        spoken_at=event.timestamp,
                        finalized=event.finalized,
                    )
                    record.add_transcript(event)
                    if conversation is not None:
                        await conversation.handle_transcript(event)

                async def interim(frame: TranscriptionFrame) -> None:
                    if conversation is not None:
                        await conversation.handle_interim(frame.text)

                if audio_enabled:
                    audio_factory = partial(
                        open_meeting_audio,
                        tts_settings=settings.tts,
                        speech_language=settings.speech.language,
                        stt_settings=settings.stt,
                        alone_leave_seconds=settings.audio_health.alone_leave_seconds,
                        on_transcript=transcript,
                        on_interim=interim,
                        on_playback_event=log_playback,
                        faults=faults,
                        journal=journal,
                        deaf_window_seconds=config.deaf_window_seconds,
                        deaf_grace_seconds=config.deaf_grace_seconds,
                        stt_keywords=speech_keywords(profile, config.twin_name)
                        if profile
                        else None,
                    )
                preflight_kwargs: dict[str, Any] = (
                    {
                        "preflight_hosts": preflight_hosts(
                            settings, reply=conversation_enabled, memory=memory
                        ),
                        "preflight_budget_seconds": config.preflight_budget_seconds,
                    }
                    if audio_enabled
                    else {}
                )
                if session_result is None:
                    with stop_signal_handlers(stop):
                        session_result = await run_meeting_session(
                            config,
                            stop=stop,
                            open_audio=audio_factory,
                            initial_speech=initial_speech,
                            conversation=conversation,
                            faults=faults,
                            journal=journal,
                            **preflight_kwargs,
                        )
                result = session_result
                exit_code = (
                    1
                    if result.terminal_reason is None
                    else FAILURE_EXIT_CODES.get(result.terminal_reason, 0)
                )
                journal.record(
                    "session_finished",
                    source="cli",
                    phase=result.phase,
                    terminal_reason=result.terminal_reason,
                    exit_code=exit_code,
                )
                try:
                    record.close()
                except OSError as error:
                    faults.record(classify_exception("meeting_record", error))
                    if record_error is None:
                        record_error = f"meeting record not finalized: {type(error).__name__}"
                if profile is not None and profile.ingest_transcripts and record_error is None:
                    try:
                        digest = hash_meeting_record(record_path)
                        finalized = load_meeting_record(record_path)
                        window_count = len(
                            meeting_windows(
                                finalized,
                                window_utterances=settings.memory.ingestion.window_utterances,
                                window_seconds=settings.memory.ingestion.window_seconds,
                            )
                        )
                        write_status(
                            status_path(record_path),
                            IngestionStatus(
                                meeting_id=resolved_meeting_id,
                                group_id=profile.memory_group,
                                status="pending",
                                windows=window_count,
                                record_sha256=digest,
                                updated_at=datetime.now(UTC),
                            ),
                        )
                    except (OSError, ValueError) as error:
                        faults.record(classify_exception("meeting_record", error))
                        record_error = f"ingestion status not written: {type(error).__name__}"
                if ingest_requested:
                    ingestion_outcome = "pending" if record_error is None else "unknown"
                elif ingestion_outcome != "unknown":
                    ingestion_outcome = "not_requested"
                final_exit = exit_code
                if record_error is not None and final_exit == 0:
                    final_exit = 1
                journal.record(
                    "cli_exited",
                    source="cli",
                    terminal_reason=result.terminal_reason,
                    exit_code=final_exit,
                )
    except RecordLockError as error:
        raise CliError(f"meeting record is locked: {error}") from None
    resume_command = (
        f"fakeyou memory-ingest --meeting {resolved_meeting_id} --persona {persona_path}"
        if ingestion_outcome == "pending" and persona_path is not None
        else None
    )
    print(
        json.dumps(
            {
                "meeting_id": resolved_meeting_id,
                "phase": result.phase,
                "terminal_reason": result.terminal_reason,
                "detail": result.detail,
                "faults": result.faults,
                "session_outcome": "completed" if exit_code == 0 else "failed",
                "ingestion_outcome": ingestion_outcome,
                "record_error": record_error,
                "resume_command": resume_command,
            },
            indent=2,
        )
    )
    if final_exit != 0:
        raise SystemExit(final_exit)
