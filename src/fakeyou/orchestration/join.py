import asyncio
import shlex
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from graphiti_core import Graphiti

from fakeyou.audio.audio import (
    SpeechPipelineSettings,
    StreamCallbacks,
    StreamComponents,
    StreamPolicy,
    open_meeting_audio,
)
from fakeyou.audio.deaf import DeafTiming
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.audio.stream_capture import (
    STREAM_FLEET_SLOTS,
    FleetTopology,
    SlotFactories,
    open_stream_fleet,
)
from fakeyou.audio.voice_activity import SlotBinder, VoiceActivityTracker
from fakeyou.catalog.commands import Command
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.conversation.meeting_turns import MeetingTurns
from fakeyou.conversation.session import MeetingAudio, SessionResult
from fakeyou.conversation.turn_graph import ConversationPhase
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.meet.browser import profile_lock
from fakeyou.memory.ingestion import meeting_windows
from fakeyou.memory.persona import speech_keywords
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.orchestration.assembly import (
    AgentWiring,
    TurnBehavior,
    VoiceprintLive,
    open_conversation,
    open_voiceprint,
    stop_signal_handlers,
)
from fakeyou.orchestration.join_context import PreparedJoin, guard_join, prepare_join
from fakeyou.orchestration.join_preflight import preflight_join
from fakeyou.orchestration.join_types import IngestResume, JoinOutcome, JoinPorts, JoinRequest
from fakeyou.orchestration.meet_actions import MeetActionDispatcher
from fakeyou.orchestration.perception import PerceptionStack, create_perception_stack
from fakeyou.orchestration.providers import preflight_hosts
from fakeyou.orchestration.runtime import SessionWiring
from fakeyou.orchestration.transcript import (
    LiveScoring,
    TranscriptSinks,
    build_transcript_callback,
)
from fakeyou.perception.roster import MeetRoster
from fakeyou.persistence.audio_persist import SlotAudioWriter
from fakeyou.persistence.brief import MeetingContext, context_path
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    finalized_prefix,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.layout import MeetingDir
from fakeyou.persistence.record import MeetingRecord, ResumeEntry, load_meeting_record


@dataclass(slots=True)
class _JoinRun:
    journal: Journal
    faults: FaultReporter
    record: MeetingRecord
    resuming: bool = False
    context_reused: bool = False
    graphiti: Graphiti | None = None
    session_result: SessionResult | None = None
    pending_error: FakeYouError | None = None
    conversation: MeetingTurns | None = None
    voiceprint_live: VoiceprintLive | None = None
    voice_activity: VoiceActivityTracker | None = None
    meet_dispatcher: MeetActionDispatcher | None = None
    perception: PerceptionStack | None = None
    audio_factory: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None
    ingest_requested: bool = False
    ingestion_outcome: str = "not_requested"
    record_error: str | None = None
    exit_code: ExitCode = ExitCode.FAILURE
    final_exit: ExitCode = ExitCode.FAILURE
    speaking: str | None = None


def _write_pending_status(
    prepared: PreparedJoin, faults: FaultReporter
) -> tuple[bool, str, str | None]:
    ingest_requested = prepared.profile is not None and prepared.profile.ingest_transcripts
    ingestion_outcome = "pending" if ingest_requested else "not_requested"
    record_error: str | None = None
    if prepared.profile is not None:
        try:
            write_status(
                status_path(prepared.record_path),
                IngestionStatus(
                    meeting_id=prepared.config.meeting_id,
                    group_id=prepared.profile.memory_group,
                    status="pending" if ingest_requested else "not_requested",
                    updated_at=datetime.now(UTC),
                ),
            )
        except OSError as error:
            faults.record(classify_exception("meeting_record", error))
            record_error = f"ingestion status not written: {type(error).__name__}"
            ingestion_outcome = "unknown"
    return ingest_requested, ingestion_outcome, record_error


def _open_join_resources(
    prepared: PreparedJoin,
    resources: AsyncExitStack,
    *,
    resuming: bool,
    context_reused: bool,
) -> _JoinRun:
    journal = Journal(
        prepared.config.meeting_id,
        file=JournalFile(
            journal_path(prepared.request.settings.paths.journal_dir, prepared.config.meeting_id)
        ),
    )
    resources.callback(journal.close)
    faults = FaultReporter(journal=journal)
    ingest_requested, ingestion_outcome, record_error = _write_pending_status(prepared, faults)
    record = MeetingRecord(prepared.record_path, faults=faults, journal=journal)
    resources.callback(record.close)
    if resuming:
        attempt = _resume_attempt(prepared.record_path)
        journal.record("session_resumed", source="cli", attempt=attempt)
        record.add_resume(attempt=attempt)
    if prepared.meeting_context is not None:
        journal.record(
            "brief_recorded",
            source="cli",
            **_brief_metadata(prepared.meeting_context, reused=context_reused),
        )
    return _JoinRun(
        journal=journal,
        faults=faults,
        record=record,
        resuming=resuming,
        context_reused=context_reused,
        ingest_requested=ingest_requested,
        ingestion_outcome=ingestion_outcome,
        record_error=record_error,
    )


async def _open_join_memory(
    prepared: PreparedJoin, run: _JoinRun, resources: AsyncExitStack, ports: JoinPorts
) -> None:
    if not (prepared.request.memory and prepared.profile is not None):
        return
    try:
        run.graphiti = await resources.enter_async_context(
            ports.open_memory(prepared.request.settings)
        )
    except ProviderFault as fault:
        run.faults.record(fault)
        run.session_result = SessionResult(
            "ended",
            "infra_failure",
            str(FakeYouError(ErrorCode.MEMORY_UNAVAILABLE, reason=fault)),
            run.faults.report(),
        )


def share_roster(registry: SpeakerRegistry, roster: MeetRoster) -> None:
    people = roster.people()
    registry.update_roster(people)
    registry.note_roster({"id": device, "name": name} for device, name in people.items() if name)
    registry.note_audio_sources(roster.audio_sources())


def _build_join_layers(
    prepared: PreparedJoin, run: _JoinRun, resources: AsyncExitStack
) -> SpeakerRegistry:
    registry = SpeakerRegistry(capacity=STREAM_FLEET_SLOTS)
    run.voiceprint_live = resources.enter_context(open_voiceprint(prepared.request.settings))
    run.voice_activity = (
        VoiceActivityTracker(registry=registry)
        if prepared.request.settings.conversation.voice_labels_enabled
        and prepared.request.audio_enabled
        else None
    )
    run.meet_dispatcher = (
        MeetActionDispatcher(
            prepared.record_path,
            meeting_id=prepared.config.meeting_id,
            enabled=prepared.config.meet_actions_enabled,
            chat_per_minute=prepared.request.settings.conversation.meet_chat_per_minute,
            journal=run.journal,
        )
        if prepared.config.meet_actions_enabled and prepared.request.conversation_enabled
        else None
    )
    run.perception = create_perception_stack(
        prepared.request.settings,
        journal=run.journal,
        record=run.record,
        screens_dir=MeetingDir.from_record_path(prepared.record_path).screens_dir,
        on_roster=partial(share_roster, registry),
    )
    return registry


async def _open_join_conversation(
    prepared: PreparedJoin, run: _JoinRun, resources: AsyncExitStack
) -> None:
    def log_transition(
        from_phase: ConversationPhase, to_phase: ConversationPhase, detail: str
    ) -> None:
        run.journal.record(
            "conversation_transitioned",
            source="agent",
            from_phase=from_phase,
            to_phase=to_phase,
            detail=detail,
        )

    if not (prepared.request.conversation_enabled and run.session_result is None):
        return
    run.conversation = await resources.enter_async_context(
        open_conversation(
            prepared.request.settings,
            prepared.config,
            agent=AgentWiring(
                profile=prepared.profile,
                brief=prepared.brief,
                graphiti=run.graphiti,
                perception=run.perception.context if run.perception is not None else None,
                history=run.record,
            ),
            behavior=TurnBehavior(
                on_transition=log_transition,
                wake_phrases=[
                    phrase.strip() for phrase in (prepared.request.wake_phrase or "").split(",")
                ],
                twin_names=[prepared.config.twin_name],
                barge_in=prepared.request.barge_in,
                on_tool_result=run.record.add_tool_result,
                meet_dispatcher=run.meet_dispatcher,
                others_speaking=(
                    run.voice_activity.others_speaking if run.voice_activity is not None else None
                ),
            ),
            diagnostics=Diagnostics(faults=run.faults, journal=run.journal),
        )
    )


def _log_playback_event(run: _JoinRun, event: PlaybackEvent) -> None:
    if run.voice_activity is not None:
        run.voice_activity.observe_self_speech(active=event.kind == "playback_started")
    if event.cue:
        run.journal.record(
            event.kind.replace("playback_", "thinking_cue_"), source="playback", detail=event.detail
        )
        return
    run.journal.record(
        event.kind,
        source="playback",
        ends_with_question=event.text.rstrip().endswith("?"),
        detail=event.detail,
    )
    if event.kind == "playback_started":
        run.speaking = event.text
    elif event.kind == "playback_finished":
        run.speaking = None
        run.record.add_twin_speech(event.text)
    elif event.kind == "playback_dropped" and event.text == run.speaking:
        run.speaking = None
        run.record.add_twin_speech(event.text, interrupted=True)


def _build_audio_factory(
    prepared: PreparedJoin, run: _JoinRun, resources: AsyncExitStack, registry: SpeakerRegistry
) -> None:
    log_playback = partial(_log_playback_event, run)
    stream_binder = SlotBinder(capacity=STREAM_FLEET_SLOTS, registry=registry)
    transcript = build_transcript_callback(
        sinks=TranscriptSinks(
            journal=run.journal,
            record=run.record,
            conversation=run.conversation.ingress if run.conversation is not None else None,
            faults=run.faults,
        ),
        binder=stream_binder,
        voice_activity=run.voice_activity,
        scoring=LiveScoring(run.voiceprint_live, registry),
        per_stream_enabled=prepared.request.settings.conversation.per_stream_enabled,
    )

    audio_recorder: SlotAudioWriter | None = None
    if (
        prepared.request.settings.audio.persist_enabled
        and prepared.request.settings.conversation.per_stream_enabled
    ):
        audio_recorder = SlotAudioWriter(
            MeetingDir.from_record_path(prepared.record_path).audio_dir, journal=run.journal
        )
        resources.callback(audio_recorder.close)
    if not prepared.request.audio_enabled:
        return
    stt_keywords = (
        speech_keywords(prepared.profile, prepared.config.twin_name) if prepared.profile else None
    )
    audio_callbacks = StreamCallbacks(
        transcript, transcript.on_interim, transcript.on_vad, log_playback
    )
    audio_diagnostics = Diagnostics(faults=run.faults, journal=run.journal)
    audio_policy = StreamPolicy(
        timing=DeafTiming(
            deaf_window_seconds=prepared.config.deaf_window_seconds,
            deaf_grace_seconds=prepared.config.deaf_grace_seconds,
            alone_leave_seconds=prepared.request.settings.audio_health.alone_leave_seconds,
        ),
        speech=SpeechPipelineSettings(
            stt_settings=prepared.request.settings.stt,
            stt_keywords=stt_keywords,
            tts_settings=prepared.request.settings.tts,
            speech_language=prepared.request.settings.speech.language,
            pactl_timeout_seconds=prepared.request.settings.audio.pactl_timeout_seconds,
        ),
    )
    if prepared.request.settings.conversation.per_stream_enabled:
        run.audio_factory = partial(
            open_stream_fleet,
            topology=FleetTopology(stream_binder, STREAM_FLEET_SLOTS, audio_recorder),
            callbacks=audio_callbacks,
            factories=SlotFactories(
                turn_tap=(
                    run.voiceprint_live.tap_for_slot if run.voiceprint_live is not None else None
                )
            ),
            diagnostics=audio_diagnostics,
            policy=audio_policy,
        )
    else:
        run.audio_factory = partial(
            open_meeting_audio,
            callbacks=audio_callbacks,
            components=StreamComponents(
                voice_activity=run.voice_activity,
                turn_tap=(run.voiceprint_live.tap if run.voiceprint_live is not None else None),
            ),
            diagnostics=audio_diagnostics,
            policy=audio_policy,
        )


async def _drive_join_session(
    prepared: PreparedJoin, run: _JoinRun, stop: asyncio.Event, ports: JoinPorts
) -> SessionResult:
    if run.session_result is not None:
        return run.session_result
    preflight_kwargs: dict[str, Any] = (
        {
            "preflight_hosts": preflight_hosts(
                prepared.request.settings,
                reply=prepared.request.conversation_enabled,
                memory=prepared.request.memory,
            ),
            "preflight_budget_seconds": prepared.config.preflight_budget_seconds,
        }
        if prepared.request.audio_enabled
        else {}
    )
    try:
        result = await ports.run_session(
            prepared.config,
            SessionWiring(
                stop=stop,
                open_audio=run.audio_factory,
                initial_speech=prepared.request.initial_speech,
                conversation=run.conversation,
                faults=run.faults,
                journal=run.journal,
                voice_activity=run.voice_activity,
                meet_dispatcher=run.meet_dispatcher,
                perception=run.perception,
                on_phase=prepared.request.on_phase,
                **preflight_kwargs,
            ),
        )
    except FakeYouError as error:
        run.pending_error = error
        result = SessionResult(
            "ended",
            ("infra_failure" if error.exit_code is ExitCode.INFRA_FAILURE else "browser_failure"),
            str(error),
            run.faults.report(),
        )
    run.session_result = result
    return result


def _write_final_ingestion_status(prepared: PreparedJoin, run: _JoinRun) -> None:
    if (
        prepared.profile is None
        or not prepared.profile.ingest_transcripts
        or run.record_error is not None
    ):
        return
    try:
        digest, byte_count = finalized_prefix(prepared.record_path)
        finalized = load_meeting_record(prepared.record_path)
        window_count = len(
            meeting_windows(
                finalized,
                window_utterances=prepared.request.settings.memory.ingestion.window_utterances,
                window_seconds=prepared.request.settings.memory.ingestion.window_seconds,
            )
        )
        write_status(
            status_path(prepared.record_path),
            IngestionStatus(
                meeting_id=prepared.config.meeting_id,
                group_id=prepared.profile.memory_group,
                status="pending",
                windows=window_count,
                record_sha256=digest,
                record_bytes=byte_count,
                updated_at=datetime.now(UTC),
            ),
        )
    except (OSError, ValueError) as error:
        run.faults.record(classify_exception("meeting_record", error))


def _finalize_join_record(prepared: PreparedJoin, run: _JoinRun, result: SessionResult) -> None:
    run.exit_code = (
        ExitCode.FAILURE
        if result.terminal_reason is None
        else TERMINAL_EXIT_CODES[result.terminal_reason]
    )
    run.journal.record(
        "session_finished",
        source="cli",
        phase=result.phase,
        terminal_reason=result.terminal_reason,
        exit_code=run.exit_code,
    )
    try:
        run.record.close()
    except OSError as error:
        run.faults.record(classify_exception("meeting_record", error))
        if run.record_error is None:
            run.record_error = f"meeting record not finalized: {type(error).__name__}"
    _write_final_ingestion_status(prepared, run)
    if run.ingest_requested:
        run.ingestion_outcome = "pending" if run.record_error is None else "unknown"
    elif run.ingestion_outcome != "unknown":
        run.ingestion_outcome = "not_requested"
    run.final_exit = run.exit_code
    if run.record_error is not None and run.final_exit == 0:
        run.final_exit = ExitCode.FAILURE
    if run.pending_error is not None:
        run.final_exit = run.pending_error.exit_code
    run.journal.record(
        "cli_exited",
        source="cli",
        terminal_reason=result.terminal_reason,
        exit_code=run.final_exit,
    )


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


def _resume_attempt(record_path: Path) -> int:
    try:
        entries = load_meeting_record(record_path)
    except (OSError, ValueError):
        return 2
    return sum(isinstance(entry, ResumeEntry) for entry in entries) + 2


def resume_command(resume: IngestResume | None) -> str | None:
    if resume is None:
        return None
    return shlex.join([*shlex.split(Command.MEMORY_INGEST), "--meeting-id", resume.meeting_id])


async def run_join_session(request: JoinRequest, ports: JoinPorts) -> JoinOutcome:
    preflight_join(request)
    prepared = await prepare_join(request)
    stop = asyncio.Event()
    with stop_signal_handlers(stop) as signal_handlers:
        with ExitStack() as guards:
            if request.profile_dir:
                guards.enter_context(profile_lock(Path(request.profile_dir)))
            with record_lock(prepared.record_path, purpose="meeting"):
                resuming, context_reused = await guard_join(prepared, ports)
                async with AsyncExitStack() as resources:
                    run = _open_join_resources(
                        prepared, resources, resuming=resuming, context_reused=context_reused
                    )
                    await _open_join_memory(prepared, run, resources, ports)
                    registry = _build_join_layers(prepared, run, resources)
                    run.journal.record(
                        "signal_handlers_bound",
                        source="cli",
                        signals=list(signal_handlers.installed),
                        unavailable=list(signal_handlers.unavailable),
                    )
                    await _open_join_conversation(prepared, run, resources)
                    _build_audio_factory(prepared, run, resources, registry)
                    result = await _drive_join_session(prepared, run, stop, ports)
                    _finalize_join_record(prepared, run, result)
    return _join_outcome(prepared, run, result)


def _join_outcome(prepared: PreparedJoin, run: _JoinRun, result: SessionResult) -> JoinOutcome:
    persona = prepared.request.identity.persona_path
    meeting_context = prepared.meeting_context
    return JoinOutcome(
        meeting_id=prepared.config.meeting_id,
        session=result,
        error_code=run.pending_error.code if run.pending_error is not None else None,
        session_completed=run.exit_code == ExitCode.SUCCESS,
        ingestion_outcome=run.ingestion_outcome,
        record_error=run.record_error,
        brief_sha256=meeting_context.brief_sha256 if meeting_context is not None else None,
        context_path=context_path(prepared.record_path) if meeting_context is not None else None,
        ingest_resume=IngestResume(
            meeting_id=prepared.config.meeting_id,
            persona=persona,
            owner_id=prepared.config.owner_id,
            twin_id=prepared.config.twin_id,
        )
        if run.ingestion_outcome == "pending" and persona is not None
        else None,
        exit_code=run.final_exit,
    )
