import asyncio
import json
import os
import sqlite3
import sys
import uuid
from contextlib import AsyncExitStack, ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from graphiti_core import Graphiti
from pipecat.frames.frames import TranscriptionFrame
from psycopg import Error as PsycopgError
from pydantic import ValidationError

from fakeyou.assembly import open_conversation, stop_signal_handlers
from fakeyou.attribution_probe import PROBE_FILE_NAME, AttributionProbe
from fakeyou.audio import open_meeting_audio
from fakeyou.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    load_meeting_brief,
    read_meeting_context,
    write_meeting_context,
)
from fakeyou.browser import ProfileInUseError, profile_lock
from fakeyou.checkpoint import checkpoint_dsn, open_checkpointer
from fakeyou.cli.errors import FAILURE_EXIT_CODES, CliError
from fakeyou.cli.memory import load_operator_persona
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    conversation_thread_id,
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
from fakeyou.lifecycle import meeting_thread_id
from fakeyou.participant_observer import EVIDENCE_FILE_NAME
from fakeyou.persona import PersonaProfile, meeting_tool_names, persona_digest, speech_keywords
from fakeyou.playback import PlaybackEvent
from fakeyou.provider_faults import ProviderFault, classify_exception
from fakeyou.providers import memory_config, open_persona_memory, preflight_hosts
from fakeyou.record import MeetingRecord, ResumeEntry, load_meeting_record, meeting_record_path
from fakeyou.report import describe_session
from fakeyou.runtime import run_meeting_session
from fakeyou.session import SessionConfig, SessionResult
from fakeyou.settings import (
    Settings,
)
from fakeyou.voice_activity import VoiceActivityTracker


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
    brief_path: Path | None = None,
    resume: bool = False,
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
    if brief_path is not None and not conversation_enabled:
        raise CliError("--brief requires --converse")
    if brief_path is not None and persona_path is None:
        raise CliError("--brief requires --persona")
    if brief_path is not None and not meeting_id:
        raise CliError("--brief requires an explicit --meeting-id")
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
        else settings.paths.browser_profile_dir / resolved_twin_id / resolved_meeting_id,
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
        checkpoint_dsn=checkpoint_dsn(settings),
        resume=resume,
    )
    profile: PersonaProfile | None = None
    brief: MeetingBrief | None = None
    meeting_context: MeetingContext | None = None
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
        if brief_path is not None:
            brief = await _load_brief(brief_path, config)
            try:
                meeting_tool_names(profile, brief, memory_requested=memory)
            except ValueError:
                raise CliError("--memory is not allowed with a brief_only meeting brief") from None
            meeting_context = MeetingContext(
                schema_version="meeting_context/v1",
                brief=brief,
                brief_sha256=brief_digest(brief),
                persona_sha256=persona_digest(profile),
            )
        if not twin_name:
            config = replace(config, twin_name=profile.display_name)
    record_path = meeting_record_path(settings.paths.record_dir, resolved_meeting_id)
    probe_path = (
        record_path.parent / PROBE_FILE_NAME
        if settings.conversation.attribution_probe_enabled
        else None
    )
    config = replace(
        config,
        participant_policy_enabled=settings.conversation.participant_policy_enabled,
        participant_evidence_path=record_path.parent / EVIDENCE_FILE_NAME,
        attribution_probe_path=probe_path,
    )
    ingest_requested = profile is not None and profile.ingest_transcripts
    ingestion_outcome = "pending" if ingest_requested else "not_requested"
    record_error: str | None = None
    result = SessionResult("ended", None, "", None)
    exit_code = 1
    final_exit = 1
    try:
        with ExitStack() as guards:
            if profile_dir:
                try:
                    guards.enter_context(profile_lock(Path(profile_dir)))
                except ProfileInUseError as error:
                    raise CliError(str(error)) from None
            with record_lock(record_path, purpose="meeting"):
                session_state = describe_session(settings.paths.journal_dir, resolved_meeting_id)
                if session_state == "unfinished" and not resume:
                    raise CliError(
                        f"meeting {resolved_meeting_id} has an unfinished session; "
                        "rejoin with --resume"
                    )
                if session_state == "finished" and resume:
                    raise CliError(
                        f"meeting {resolved_meeting_id} already ended; use a new meeting ID"
                    )
                resuming = resume and (
                    session_state == "unfinished" or await _has_live_thread(config)
                )
                context_reused = await _settle_meeting_context(record_path, config, meeting_context)
                conversation: ConversationLoop | None = None
                graphiti: Graphiti | None = None
                session_result: SessionResult | None = None
                async with AsyncExitStack() as resources:
                    journal = Journal(
                        resolved_meeting_id,
                        path=journal_path(settings.paths.journal_dir, resolved_meeting_id),
                    )
                    resources.callback(journal.close)
                    faults = FaultReporter(journal=journal)
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
                    resources.callback(record.close)
                    if resuming:
                        attempt = _resume_attempt(record_path)
                        journal.record("session_resumed", source="cli", attempt=attempt)
                        record.add_resume(attempt=attempt)
                    if meeting_context is not None:
                        journal.record(
                            "brief_recorded",
                            source="cli",
                            **_brief_metadata(meeting_context, reused=context_reused),
                        )
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

                    voice_activity = (
                        VoiceActivityTracker(twin_name=config.twin_name)
                        if settings.conversation.voice_labels_enabled and audio_enabled
                        else None
                    )

                    def log_playback(event: PlaybackEvent) -> None:
                        if voice_activity is not None:
                            voice_activity.observe_self_speech(event.kind == "playback_started")
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
                                brief=brief,
                            )
                        )

                    async def transcript(frame: TranscriptionFrame) -> None:
                        attribution = (
                            voice_activity.attribute() if voice_activity is not None else None
                        )
                        event = (
                            transcript_event(frame, "unknown", None)
                            if attribution is None
                            else transcript_event(
                                frame, attribution.participant_id, attribution.evidence_id
                            )
                        )
                        journal.record(
                            "transcript_finalized",
                            source="stt",
                            origin=event.source,
                            transcript_id=event.id,
                            participant_id=event.participant_id,
                            speaker_evidence_id=event.speaker_evidence_id,
                            spoken_at=event.timestamp,
                            finalized=event.finalized,
                            **(attribution.journal_fields() if attribution is not None else {}),
                        )
                        record.add_transcript(event)
                        if conversation is not None:
                            await conversation.handle_transcript(event)

                    async def interim(frame: TranscriptionFrame) -> None:
                        if conversation is not None:
                            await conversation.handle_interim(frame.text)

                    if audio_enabled:
                        audio_probe = (
                            AttributionProbe(config.attribution_probe_path)
                            if config.attribution_probe_path is not None
                            else None
                        )
                        audio_factory = partial(
                            open_meeting_audio,
                            probe=audio_probe,
                            voice_activity=voice_activity,
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
                                voice_activity=voice_activity,
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
                "brief_sha256": meeting_context.brief_sha256
                if meeting_context is not None
                else None,
                "context_path": str(context_path(record_path))
                if meeting_context is not None
                else None,
            },
            indent=2,
        )
    )
    if final_exit != 0:
        raise SystemExit(final_exit)


async def _load_brief(path: Path, config: SessionConfig) -> MeetingBrief:
    try:
        return await asyncio.to_thread(
            load_meeting_brief,
            path,
            owner_id=config.owner_id,
            twin_id=config.twin_id,
            meeting_id=config.meeting_id,
        )
    except OSError as error:
        raise CliError(f"--brief cannot be read: {error.strerror}") from None
    except ValidationError as error:
        kinds = sorted(
            {item["type"] for item in error.errors(include_url=False, include_input=False)}
        )
        raise CliError(
            f"--brief is invalid: {error.error_count()} validation error(s): {', '.join(kinds)}"
        ) from None
    except ValueError as error:
        raise CliError(f"--brief is invalid: {error}") from None


def _brief_metadata(context: MeetingContext, *, reused: bool) -> dict[str, Any]:
    brief = context.brief
    return {
        "brief_sha256": context.brief_sha256,
        "persona_sha256": context.persona_sha256,
        "knowledge_scope": brief.knowledge_scope,
        "permissions": sorted(brief.permissions),
        "agenda_count": len(brief.agenda),
        "objective_count": len(brief.objectives),
        "approved_fact_count": len(brief.approved_facts),
        "question_count": len(brief.questions),
        "objective_ids": [item.id for item in brief.objectives],
        "question_ids": [item.id for item in brief.questions],
        "reused": reused,
    }


async def _settle_meeting_context(
    record_path: Path, config: SessionConfig, context: MeetingContext | None
) -> bool:
    path = context_path(record_path)
    try:
        existing = read_meeting_context(path)
    except (OSError, ValueError):
        raise CliError(
            f"meeting context is unreadable or corrupt at {path}; use a new meeting ID"
        ) from None
    if context is None:
        if existing is not None:
            raise CliError(
                f"meeting {config.meeting_id} was started with --brief; "
                "rejoin with the same --brief and --persona"
            )
        return False
    if existing is not None:
        if existing != context:
            raise CliError(
                "meeting context differs from --brief or --persona; use a new meeting ID"
            )
        return True
    thread_id = conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id)
    if await _has_conversation(
        record_path, config.checkpoint_path, thread_id, dsn=config.checkpoint_dsn
    ):
        raise CliError(
            f"meeting {config.meeting_id} already has conversation data without a meeting "
            "context; use a new meeting ID for --brief"
        )
    try:
        write_meeting_context(path, context)
    except (OSError, ValueError) as error:
        raise CliError(f"meeting context not written: {type(error).__name__}") from None
    return False


async def _has_conversation(
    record_path: Path, checkpoint_path: Path, thread_id: str, dsn: str | None = None
) -> bool:
    if await asyncio.to_thread(_has_entries, record_path):
        return True
    try:
        if dsn is None and not await asyncio.to_thread(checkpoint_path.exists):
            return False
        async with open_checkpointer(checkpoint_path, dsn=dsn) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except (sqlite3.Error, OSError, PsycopgError) as error:
        raise CliError(
            f"conversation checkpoint cannot be checked: {type(error).__name__}"
        ) from None


def _has_entries(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _has_live_thread(config: SessionConfig) -> bool:
    if config.checkpoint_dsn is None and not await asyncio.to_thread(config.checkpoint_path.exists):
        return False
    try:
        async with open_checkpointer(config.checkpoint_path, dsn=config.checkpoint_dsn) as saver:
            saved = await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": meeting_thread_id(config.twin_id, config.meeting_id)
                    }
                }
            )
    except (sqlite3.Error, OSError, PsycopgError) as error:
        raise CliError(f"lifecycle checkpoint cannot be checked: {type(error).__name__}") from None
    return saved is not None and saved.checkpoint["channel_values"].get("phase") != "ended"


def _resume_attempt(record_path: Path) -> int:
    try:
        entries = load_meeting_record(record_path)
    except (OSError, ValueError):
        return 2
    return sum(isinstance(entry, ResumeEntry) for entry in entries) + 2
