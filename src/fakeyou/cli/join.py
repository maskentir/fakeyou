import asyncio
import json
import os
import sys
import uuid
from contextlib import AsyncExitStack
from dataclasses import replace
from functools import partial
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from graphiti_core import Graphiti
from pipecat.frames.frames import TranscriptionFrame

from fakeyou.assembly import open_conversation, stop_signal_handlers
from fakeyou.audio import (
    AGENT_SINK_NAME,
    MEETING_MONITOR_SOURCE_NAME,
    open_meeting_audio,
)
from fakeyou.cli.errors import FAILURE_EXIT_CODES, CliError
from fakeyou.cli.memory import ingest_until_stopped, load_operator_persona
from fakeyou.conversation import (
    ConversationLoop,
    ConversationPhase,
    transcript_event,
)
from fakeyou.faults import FaultReporter
from fakeyou.journal import Journal, journal_path
from fakeyou.persona import PersonaProfile, speech_keywords
from fakeyou.playback import PlaybackEvent
from fakeyou.provider_faults import ProviderFault, classify_exception
from fakeyou.providers import memory_config, open_persona_memory, preflight_hosts
from fakeyou.record import MeetingRecord, meeting_record_path
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
    resolved_headless = settings.meeting.headless if headless is None else headless
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
        warmup_browser=settings.meeting.warmup_browser,
        deaf_window_seconds=settings.audio_health.deaf_window_seconds,
        deaf_grace_seconds=settings.audio_health.deaf_grace_seconds,
        preflight_budget_seconds=settings.preflight.budget_seconds,
        headless=resolved_headless,
        license_key=settings.meeting.license_key.get_secret_value()
        if settings.meeting.license_key
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
    record = MeetingRecord(
        meeting_record_path(settings.paths.record_dir, resolved_meeting_id), faults=faults
    )

    conversation: ConversationLoop | None = None
    graphiti: Graphiti | None = None
    result: SessionResult | None = None
    async with AsyncExitStack() as resources:
        resources.callback(journal.close)
        resources.callback(record.close)
        if memory and profile is not None:
            try:
                graphiti = await resources.enter_async_context(open_persona_memory(settings))
            except ProviderFault as fault:
                faults.record(fault)
                result = SessionResult(
                    "ended", "infra_failure", f"memory unavailable: {fault}", faults.report()
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

        if conversation_enabled and result is None:
            conversation = await resources.enter_async_context(
                open_conversation(
                    settings,
                    config,
                    graphiti=graphiti,
                    profile=profile,
                    on_tool_result=record.add_tool_result,
                    on_transition=log_transition,
                    wake_phrases=[phrase.strip() for phrase in (wake_phrase or "").split(",")],
                    barge_in=barge_in,
                    faults=faults,
                    journal=journal,
                )
            )

        async def transcript(frame: TranscriptionFrame) -> None:
            event = transcript_event(frame)
            journal.record(
                "transcript_finalized",
                source="stt",
                origin=event.source,
                transcript_id=event.id,
                participant=event.participant,
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
                stt_keywords=speech_keywords(profile, config.twin_name) if profile else None,
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
        if result is None:
            with stop_signal_handlers(stop):
                result = await run_meeting_session(
                    config,
                    stop=stop,
                    open_audio=audio_factory,
                    initial_speech=initial_speech,
                    conversation=conversation,
                    faults=faults,
                    journal=journal,
                    **preflight_kwargs,
                )
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
        if graphiti is not None and profile is not None and profile.ingest_transcripts:
            try:
                await ingest_until_stopped(
                    graphiti,
                    meeting_record_path(settings.paths.record_dir, resolved_meeting_id),
                    profile=profile,
                    meeting_id=resolved_meeting_id,
                    twin_name=config.twin_name,
                    journal=journal,
                    faults=faults,
                    settings=settings,
                )
            except Exception as error:
                faults.record(classify_exception("memory_ingest", error))
        journal.record(
            "cli_exited",
            source="cli",
            terminal_reason=result.terminal_reason,
            exit_code=exit_code,
        )
    print(
        json.dumps(
            {
                "meeting_id": resolved_meeting_id,
                "phase": result.phase,
                "terminal_reason": result.terminal_reason,
                "detail": result.detail,
                "faults": getattr(result, "faults", None),
            },
            indent=2,
        )
    )
    if exit_code != 0:
        raise SystemExit(exit_code)
