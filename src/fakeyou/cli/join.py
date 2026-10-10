import asyncio
import os
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from graphiti_core import Graphiti
from pipecat.frames.frames import TranscriptionFrame
from pydantic import ValidationError

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
from fakeyou.audio.voice_activity import SlotBinder, VoiceActivityTracker, voice_participant_id
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.cli._operator import OperatorIdentity, load_operator_persona
from fakeyou.cli.output import emit_json
from fakeyou.config.meeting_request import MeetingRequest, validate_meeting_request
from fakeyou.config.settings import (
    Settings,
)
from fakeyou.conversation.conversation import (
    ConversationLoop,
    ConversationPhase,
    conversation_thread_id,
)
from fakeyou.conversation.session import MeetingAudio, SessionConfig, SessionResult
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url
from fakeyou.foundation.attribution_probe import PROBE_FILE_NAME, AttributionProbe
from fakeyou.foundation.invariants import AGENT_SINK_NAME, MEETING_MONITOR_SOURCE_NAME
from fakeyou.foundation.transcript import (
    CaptureContext,
    TranscriptEvent,
    merge_transcripts,
    transcript_event,
    transcript_stamp,
)
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import VerificationInputs, claim_of
from fakeyou.identity.voiceprint_match import (
    VoiceScoreAttribution,
    apply_voice_scores,
    score_turn_audio,
)
from fakeyou.meet.browser import profile_lock
from fakeyou.meet.participant_observer import EVIDENCE_FILE_NAME
from fakeyou.memory.ingestion import meeting_windows
from fakeyou.memory.persona import (
    PersonaProfile,
    meeting_tool_names,
    persona_digest,
    speech_keywords,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter, record_consumer_fault
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.observability.report import describe_session
from fakeyou.orchestration.assembly import (
    AgentWiring,
    LoopBehavior,
    VoiceprintLive,
    create_visual_captioner,
    open_conversation,
    open_voiceprint,
    stop_signal_handlers,
)
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.orchestration.providers import memory_config, open_persona_memory, preflight_hosts
from fakeyou.orchestration.runtime import (
    MeetActionDispatcher,
    SessionWiring,
    run_meeting_session,
)
from fakeyou.persistence.audio_persist import SlotAudioWriter
from fakeyou.persistence.brief import (
    MeetingBrief,
    MeetingContext,
    brief_digest,
    context_path,
    load_meeting_brief,
    read_meeting_context,
    write_meeting_context,
)
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    hash_meeting_record,
    record_lock,
    status_path,
    write_status,
)
from fakeyou.persistence.record import (
    MeetingRecord,
    ResumeEntry,
    VisualDraft,
    load_meeting_record,
    meeting_record_path,
)
from fakeyou.visual.visual_caption import VisualCaption, VisualCaptioner, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore, visual_refs_for_utterance
from fakeyou.worker.worker_backend import memory_ingest_args


def stream_slot(stream_id: str) -> int | None:
    raw = stream_id.removeprefix("slot-")
    if raw == stream_id or not raw.isdigit():
        return None
    return int(raw)


def attribute_stream_frame(
    frame: TranscriptionFrame, binder: SlotBinder | None
) -> tuple[str, str | None, str, dict[str, Any], float, str | None]:
    raw_capture = getattr(frame, "capture_ms", 0.0)
    capture_ms = (
        float(raw_capture)
        if isinstance(raw_capture, (int, float)) and not isinstance(raw_capture, bool)
        else 0.0
    )
    source = getattr(frame, "transport_source", "") or ""
    stream_id = source if isinstance(source, str) else ""
    slot = stream_slot(stream_id)
    roster_size = binder.roster_size if binder is not None else 0
    if binder is None or slot is None:
        extra: dict[str, Any] = {
            "speaker_reason": "no_source",
            "speaker_slot": slot,
            "speaker_bound": False,
            "roster_size": roster_size,
        }
        if binder is not None:
            drained = binder.drain_binder_events()
            if drained:
                extra["tenure_events"] = drained
        return ("unknown", None, stream_id, extra, capture_ms, None)
    outcome = binder.resolve(slot)
    drained = binder.drain_binder_events()
    csrcs = list(binder.stream_csrcs(slot))
    if isinstance(outcome, str):
        extra = {
            "speaker_reason": outcome,
            "speaker_slot": slot,
            "speaker_bound": False,
            "roster_size": roster_size,
        }
        if csrcs:
            extra["speaker_csrc"] = csrcs
        if drained:
            extra["tenure_events"] = drained
        return ("unknown", None, stream_id, extra, capture_ms, None)
    extra = {
        "speaker_reason": "stream_slot",
        "speaker_slot": slot,
        "speaker_bound": outcome.device is not None,
        "roster_size": roster_size,
    }
    extra["speaker_uuid"] = outcome.uuid
    if csrcs:
        extra["speaker_csrc"] = csrcs
    if drained:
        extra["tenure_events"] = drained
    return (
        voice_participant_id(outcome.label),
        f"stream-{slot}",
        stream_id,
        extra,
        capture_ms,
        outcome.device,
    )


async def emit_transcript(
    event: TranscriptEvent,
    *,
    journal: Journal,
    record: MeetingRecord,
    conversation: ConversationLoop | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    journal.record(
        "transcript_finalized",
        source="stt",
        origin=event.source,
        transcript_id=event.id,
        participant_id=event.participant_id,
        speaker_evidence_id=event.speaker_evidence_id,
        spoken_at=event.timestamp,
        finalized=event.finalized,
        stream_id=event.stream_id,
        **(extra or {}),
    )
    record.add_transcript(event)
    if conversation is not None:
        await conversation.handle_transcript(event)


@dataclass(frozen=True, slots=True)
class TranscriptSinks:
    journal: Journal
    record: MeetingRecord
    conversation: ConversationLoop | None = None
    faults: FaultReporter | None = None
    visual_store: VisualStore | None = None


@dataclass(frozen=True, slots=True)
class LiveScoring:
    voiceprint: VoiceprintLive | None
    registry: SpeakerRegistry


def record_visual_caption(
    record: MeetingRecord, frame: VisualFrame, caption: VisualCaption
) -> None:
    try:
        captured_at = datetime.fromisoformat(frame.captured_at)
    except ValueError:
        return
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=UTC)
    record.add_visual(
        VisualDraft(
            visual_id=frame.frame_id,
            caption=caption.text,
            sha256=frame.sha256,
            captured_at=captured_at,
            frame_ref=frame_ref(frame),
            captured_page_ms=frame.captured_page_ms,
        )
    )


def _claim_for_uuid(registry: SpeakerRegistry | None, speaker_uuid: Any) -> str | None:
    if registry is None or not isinstance(speaker_uuid, str) or not speaker_uuid:
        return None
    row = registry.row(speaker_uuid)
    if row is None:
        return None
    return claim_of(row)


def _visual_event(event: TranscriptEvent, visual_store: VisualStore | None) -> TranscriptEvent:
    if visual_store is None:
        return event
    return replace(
        event,
        visual_refs=visual_refs_for_utterance(
            visual_store.frames,
            capture_ms=event.capture_ms,
            spoken_at=event.timestamp,
        ),
    )


class TranscriptDrain:
    def __init__(
        self,
        *,
        sinks: TranscriptSinks,
        binder: SlotBinder,
        voice_activity: VoiceActivityTracker | None,
        scoring: LiveScoring | None = None,
        per_stream_enabled: bool = False,
    ) -> None:
        self._sinks = sinks
        self._binder = binder
        self._voice_activity = voice_activity
        self._scoring = scoring
        self._per_stream_enabled = per_stream_enabled
        self._pending: list[TranscriptEvent] = []
        self._handled: set[str] = set()
        self._extras: dict[str, dict[str, Any]] = {}
        self._emitted_max: TranscriptEvent | None = None

    async def __call__(self, frame: TranscriptionFrame) -> None:
        if self._per_stream_enabled:
            await self._per_stream(frame)
        else:
            await self._legacy(frame)

    def _registry(self) -> SpeakerRegistry | None:
        return self._scoring.registry if self._scoring is not None else None

    def _score_turn(
        self, speaker_uuid: Any, stream_id: str, device: str | None, *, finalized: bool
    ) -> None:
        scoring = self._scoring
        if scoring is None or scoring.voiceprint is None or not finalized:
            return
        voiceprint = scoring.voiceprint
        if not isinstance(speaker_uuid, str) or not speaker_uuid:
            voiceprint.tap_for(stream_id).take_clip()
            return
        try:
            scores = score_turn_audio(
                voiceprint.tap_for(stream_id).take_clip(),
                voiceprint.embedder,
                voiceprint.matcher,
                min_seconds=voiceprint.min_seconds,
            )
            if scores is None:
                return
            row = scoring.registry.row(speaker_uuid)
            claim = claim_of(row) if row is not None else None
            apply_voice_scores(
                scoring.registry,
                scores,
                attribution=VoiceScoreAttribution(speaker_uuid, device, voiceprint.owners),
                diagnostics=Diagnostics(faults=self._sinks.faults, journal=self._sinks.journal),
                verification=VerificationInputs(
                    artifact=voiceprint.artifact,
                    claim=claim,
                    slot=stream_slot(stream_id),
                    embedder=voiceprint.embedder,
                ),
            )
        except Exception as error:
            record_consumer_fault(
                self._sinks.faults,
                self._sinks.journal,
                source="audio",
                detail="voiceprint_scoring",
                error=error,
            )

    async def _drain_ordered(self) -> None:
        for ordered in merge_transcripts(self._pending):
            if ordered.id in self._handled:
                continue
            self._handled.add(ordered.id)
            emitted_max = self._emitted_max
            if emitted_max is not None and transcript_stamp(ordered) < transcript_stamp(
                emitted_max
            ):
                self._sinks.journal.record(
                    "order_inversion_detected",
                    source="stt",
                    transcript_id=ordered.id,
                    spoken_at=ordered.timestamp,
                    emitted_max_spoken_at=emitted_max.timestamp,
                    stream_id=ordered.stream_id,
                )
            if emitted_max is None or transcript_stamp(emitted_max) < transcript_stamp(ordered):
                self._emitted_max = ordered
            await emit_transcript(
                ordered,
                journal=self._sinks.journal,
                record=self._sinks.record,
                conversation=self._sinks.conversation,
                extra=self._extras.pop(ordered.id, None),
            )
        self._pending[:] = [event for event in self._pending if event.id not in self._handled]

    async def _per_stream(self, frame: TranscriptionFrame) -> None:
        participant_id, evidence_id, stream_id, extra, capture_ms, device = attribute_stream_frame(
            frame, self._binder
        )
        event = _visual_event(
            transcript_event(
                frame,
                participant_id,
                evidence_id,
                CaptureContext(
                    stream_id,
                    capture_ms=capture_ms,
                    display_claim=_claim_for_uuid(self._registry(), extra.get("speaker_uuid")),
                ),
            ),
            self._sinks.visual_store,
        )
        self._pending.append(event)
        self._extras[event.id] = extra
        await self._drain_ordered()
        self._score_turn(extra.get("speaker_uuid"), stream_id, device, finalized=frame.finalized)

    async def _legacy(self, frame: TranscriptionFrame) -> None:
        voice_activity = self._voice_activity
        attribution = voice_activity.attribute() if voice_activity is not None else None
        legacy_uuid = attribution.speaker_uuid if attribution is not None else None
        legacy_claim = _claim_for_uuid(self._registry(), legacy_uuid)
        if attribution is None:
            event = transcript_event(
                frame, "unknown", None, CaptureContext(display_claim=legacy_claim)
            )
        else:
            event = transcript_event(
                frame,
                attribution.participant_id,
                attribution.evidence_id,
                CaptureContext(display_claim=legacy_claim),
            )
        await emit_transcript(
            _visual_event(event, self._sinks.visual_store),
            journal=self._sinks.journal,
            record=self._sinks.record,
            conversation=self._sinks.conversation,
            extra=attribution.journal_fields() if attribution is not None else None,
        )
        self._score_turn(legacy_uuid, "", None, finalized=frame.finalized)


def build_transcript_callback(
    *,
    sinks: TranscriptSinks,
    binder: SlotBinder,
    voice_activity: VoiceActivityTracker | None,
    scoring: LiveScoring | None = None,
    per_stream_enabled: bool = False,
) -> Callable[[TranscriptionFrame], Awaitable[None]]:
    return TranscriptDrain(
        sinks=sinks,
        binder=binder,
        voice_activity=voice_activity,
        scoring=scoring,
        per_stream_enabled=per_stream_enabled,
    )


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


@dataclass(frozen=True, slots=True)
class _JoinReady:
    options: JoinOptions
    settings: Settings
    config: SessionConfig
    record_path: Path
    profile: PersonaProfile | None
    brief: MeetingBrief | None
    meeting_context: MeetingContext | None
    audio_enabled: bool


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
    conversation: ConversationLoop | None = None
    voiceprint_live: VoiceprintLive | None = None
    voice_activity: VoiceActivityTracker | None = None
    meet_dispatcher: MeetActionDispatcher | None = None
    visual_store: VisualStore | None = None
    visual_captioner: VisualCaptioner | None = None
    visual_on_caption: Callable[[VisualFrame, VisualCaption], None] | None = None
    audio_factory: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None
    ingest_requested: bool = False
    ingestion_outcome: str = "not_requested"
    record_error: str | None = None
    exit_code: ExitCode = ExitCode.FAILURE
    final_exit: ExitCode = ExitCode.FAILURE


def _require_audio_platform() -> None:
    if (
        sys.platform != "linux"
        or os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME
        or os.environ.get("PULSE_SINK") != AGENT_SINK_NAME
        or find_spec("pyaudio") is None
    ):
        raise FakeYouError(ErrorCode.MEETING_AUDIO_REQUIRES_WORKER)


def _validate_join_request(link: str, options: JoinOptions, settings: Settings) -> bool:
    audio_enabled = options.audio_enabled or settings.worker.audio
    validate_meeting_request(
        MeetingRequest(
            link=link,
            lobby_timeout=options.lobby_timeout,
            duration=options.duration,
            initial_speech=options.initial_speech,
            audio=audio_enabled,
            conversation=options.conversation_enabled,
            memory=options.memory,
            persona_path=options.identity.persona_path,
            brief_path=options.brief_path,
            meeting_id=options.meeting_id,
            wake_phrase=options.wake_phrase,
            barge_in=options.barge_in,
        )
    )
    if settings.conversation.participant_policy_enabled and not (options.wake_phrase or "").strip():
        raise FakeYouError(ErrorCode.MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES)
    if options.conversation_enabled:
        settings.require_roles("reply", "reply_fallback")
    if options.memory:
        memory_config(settings)
    if audio_enabled:
        _require_audio_platform()
        settings.require_roles("stt", "tts")
    return audio_enabled


def _build_join_config(link: str, options: JoinOptions, settings: Settings) -> SessionConfig:
    resolved_meeting_id = options.meeting_id or uuid.uuid4().hex[:12]
    resolved_twin_id = options.identity.twin_id or settings.identity.twin_id
    resolved_headless = settings.browser.headless if options.headless is None else options.headless
    return SessionConfig(
        owner_id=options.identity.owner_id or settings.identity.owner_id,
        twin_id=resolved_twin_id,
        meeting_id=resolved_meeting_id,
        link=link,
        twin_name=options.twin_name or settings.identity.twin_name,
        browser_profile_dir=Path(options.profile_dir)
        if options.profile_dir
        else settings.paths.browser_profile_dir / resolved_twin_id / resolved_meeting_id,
        lobby_timeout_seconds=options.lobby_timeout
        if options.lobby_timeout is not None
        else settings.meeting.lobby_timeout_seconds,
        session_duration_seconds=options.duration
        if options.duration is not None
        else settings.meeting.session_duration_seconds,
        join_poll_interval_seconds=settings.meeting.join_poll_seconds,
        join_max_attempts=settings.meeting.join_max_attempts,
        join_retry_base_seconds=settings.meeting.join_retry_base_seconds,
        join_retry_cap_seconds=settings.meeting.join_retry_cap_seconds,
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
        database_url=configured_url(settings),
        resume=options.resume,
    )


async def _load_join_context(
    options: JoinOptions, settings: Settings, config: SessionConfig
) -> tuple[PersonaProfile | None, MeetingBrief | None, MeetingContext | None, SessionConfig]:
    profile: PersonaProfile | None = None
    brief: MeetingBrief | None = None
    meeting_context: MeetingContext | None = None
    if options.identity.persona_path is not None:
        profile = await load_operator_persona(options.identity, settings)
        if profile.language != settings.speech.language:
            raise FakeYouError(ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH)
        if options.memory and not (profile.allowed_tools or profile.ingest_transcripts):
            raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST)
        if options.brief_path is not None:
            brief = await _load_brief(options.brief_path, config)
            meeting_tool_names(profile, brief, memory_requested=options.memory)
            meeting_context = MeetingContext(
                schema_version="meeting_context/v1",
                brief=brief,
                brief_sha256=brief_digest(brief),
                persona_sha256=persona_digest(profile),
            )
        if not options.twin_name:
            config = replace(config, twin_name=profile.display_name)
    return profile, brief, meeting_context, config


def _prepare_session_paths(config: SessionConfig, settings: Settings) -> tuple[SessionConfig, Path]:
    record_path = meeting_record_path(settings.paths.record_dir, config.meeting_id)
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
        per_stream_enabled=settings.conversation.per_stream_enabled,
        meet_actions_enabled=settings.conversation.meet_actions_enabled,
        visual_enabled=settings.visual.enabled,
    )
    return config, record_path


async def _guard_join_session(ready: _JoinReady) -> tuple[bool, bool]:
    session_state = describe_session(ready.settings.paths.journal_dir, ready.config.meeting_id)
    if session_state == "unfinished" and not ready.options.resume:
        raise FakeYouError(ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=ready.config.meeting_id)
    if session_state == "finished" and ready.options.resume:
        raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=ready.config.meeting_id)
    resuming = ready.options.resume and (
        session_state == "unfinished" or await _has_live_thread(ready.config)
    )
    context_reused = await _settle_meeting_context(
        ready.record_path, ready.config, ready.meeting_context
    )
    return resuming, context_reused


def _write_pending_status(ready: _JoinReady, faults: FaultReporter) -> tuple[bool, str, str | None]:
    ingest_requested = ready.profile is not None and ready.profile.ingest_transcripts
    ingestion_outcome = "pending" if ingest_requested else "not_requested"
    record_error: str | None = None
    if ready.profile is not None:
        try:
            write_status(
                status_path(ready.record_path),
                IngestionStatus(
                    meeting_id=ready.config.meeting_id,
                    group_id=ready.profile.memory_group,
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
    ready: _JoinReady,
    resources: AsyncExitStack,
    *,
    resuming: bool,
    context_reused: bool,
) -> _JoinRun:
    journal = Journal(
        ready.config.meeting_id,
        file=JournalFile(journal_path(ready.settings.paths.journal_dir, ready.config.meeting_id)),
    )
    resources.callback(journal.close)
    faults = FaultReporter(journal=journal)
    ingest_requested, ingestion_outcome, record_error = _write_pending_status(ready, faults)
    record = MeetingRecord(ready.record_path, faults=faults)
    resources.callback(record.close)
    if resuming:
        attempt = _resume_attempt(ready.record_path)
        journal.record("session_resumed", source="cli", attempt=attempt)
        record.add_resume(attempt=attempt)
    if ready.meeting_context is not None:
        journal.record(
            "brief_recorded",
            source="cli",
            **_brief_metadata(ready.meeting_context, reused=context_reused),
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


async def _open_join_memory(ready: _JoinReady, run: _JoinRun, resources: AsyncExitStack) -> None:
    if not (ready.options.memory and ready.profile is not None):
        return
    try:
        run.graphiti = await resources.enter_async_context(open_persona_memory(ready.settings))
    except ProviderFault as fault:
        run.faults.record(fault)
        run.session_result = SessionResult(
            "ended",
            "infra_failure",
            str(FakeYouError(ErrorCode.MEMORY_UNAVAILABLE, reason=fault)),
            run.faults.report(),
        )


def _build_join_layers(
    ready: _JoinReady, run: _JoinRun, resources: AsyncExitStack
) -> SpeakerRegistry:
    registry = SpeakerRegistry(capacity=STREAM_FLEET_SLOTS)
    run.voiceprint_live = resources.enter_context(open_voiceprint(ready.settings))
    run.voice_activity = (
        VoiceActivityTracker(twin_name=ready.config.twin_name, registry=registry)
        if ready.settings.conversation.voice_labels_enabled and ready.audio_enabled
        else None
    )
    run.meet_dispatcher = (
        MeetActionDispatcher(
            ready.record_path,
            meeting_id=ready.config.meeting_id,
            enabled=ready.settings.conversation.meet_actions_enabled,
            journal=run.journal,
        )
        if ready.settings.conversation.meet_actions_enabled and ready.options.conversation_enabled
        else None
    )
    run.visual_store = (
        VisualStore(ready.record_path.parent / "visual", journal=run.journal)
        if ready.settings.visual.enabled
        else None
    )
    run.visual_captioner = (
        create_visual_captioner(ready.settings, journal=run.journal)
        if run.visual_store is not None and ready.options.conversation_enabled
        else None
    )
    run.visual_on_caption = (
        partial(record_visual_caption, run.record)
        if run.visual_store is not None and run.visual_captioner is not None
        else None
    )
    return registry


async def _open_join_conversation(
    ready: _JoinReady, run: _JoinRun, resources: AsyncExitStack
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

    if not (ready.options.conversation_enabled and run.session_result is None):
        return
    run.conversation = await resources.enter_async_context(
        open_conversation(
            ready.settings,
            ready.config,
            agent=AgentWiring(
                profile=ready.profile,
                brief=ready.brief,
                graphiti=run.graphiti,
                visual_store=run.visual_store,
                visual_captioner=run.visual_captioner,
            ),
            behavior=LoopBehavior(
                on_transition=log_transition,
                wake_phrases=[
                    phrase.strip() for phrase in (ready.options.wake_phrase or "").split(",")
                ],
                barge_in=ready.options.barge_in,
                on_tool_result=run.record.add_tool_result,
                meet_dispatcher=run.meet_dispatcher,
            ),
            diagnostics=Diagnostics(faults=run.faults, journal=run.journal),
        )
    )


def _log_playback_event(run: _JoinRun, event: PlaybackEvent) -> None:
    if run.voice_activity is not None:
        run.voice_activity.observe_self_speech(active=event.kind == "playback_started")
    run.journal.record(
        event.kind,
        source="playback",
        ends_with_question=event.text.rstrip().endswith("?"),
        detail=event.detail,
    )
    if event.kind == "playback_finished":
        run.record.add_twin_speech(event.text)


def _build_audio_factory(
    ready: _JoinReady, run: _JoinRun, resources: AsyncExitStack, registry: SpeakerRegistry
) -> None:
    log_playback = partial(_log_playback_event, run)
    stream_binder = SlotBinder(capacity=STREAM_FLEET_SLOTS, registry=registry)
    transcript = build_transcript_callback(
        sinks=TranscriptSinks(
            journal=run.journal,
            record=run.record,
            conversation=run.conversation,
            faults=run.faults,
            visual_store=run.visual_store,
        ),
        binder=stream_binder,
        voice_activity=run.voice_activity,
        scoring=LiveScoring(run.voiceprint_live, registry),
        per_stream_enabled=ready.settings.conversation.per_stream_enabled,
    )

    async def interim(frame: TranscriptionFrame) -> None:
        if run.conversation is not None:
            await run.conversation.handle_interim(frame.text)

    async def vad(*, active: bool) -> None:
        if run.conversation is not None:
            await run.conversation.note_vad(active=active)

    audio_recorder: SlotAudioWriter | None = None
    if ready.settings.audio.persist_enabled and ready.settings.conversation.per_stream_enabled:
        audio_recorder = SlotAudioWriter(ready.record_path.parent / "audio", journal=run.journal)
        resources.callback(audio_recorder.close)
    if not ready.audio_enabled:
        return
    audio_probe = (
        AttributionProbe(ready.config.attribution_probe_path)
        if ready.config.attribution_probe_path is not None
        else None
    )
    stt_keywords = speech_keywords(ready.profile, ready.config.twin_name) if ready.profile else None
    audio_callbacks = StreamCallbacks(transcript, interim, vad, log_playback)
    audio_diagnostics = Diagnostics(faults=run.faults, journal=run.journal)
    audio_policy = StreamPolicy(
        timing=DeafTiming(
            deaf_window_seconds=ready.config.deaf_window_seconds,
            deaf_grace_seconds=ready.config.deaf_grace_seconds,
            alone_leave_seconds=ready.settings.audio_health.alone_leave_seconds,
        ),
        speech=SpeechPipelineSettings(
            stt_settings=ready.settings.stt,
            stt_keywords=stt_keywords,
            tts_settings=ready.settings.tts,
            speech_language=ready.settings.speech.language,
        ),
    )
    if ready.settings.conversation.per_stream_enabled:
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
                probe=audio_probe,
                voice_activity=run.voice_activity,
                turn_tap=(run.voiceprint_live.tap if run.voiceprint_live is not None else None),
            ),
            diagnostics=audio_diagnostics,
            policy=audio_policy,
        )


async def _drive_join_session(
    ready: _JoinReady, run: _JoinRun, stop: asyncio.Event
) -> SessionResult:
    if run.session_result is not None:
        return run.session_result
    preflight_kwargs: dict[str, Any] = (
        {
            "preflight_hosts": preflight_hosts(
                ready.settings,
                reply=ready.options.conversation_enabled,
                memory=ready.options.memory,
            ),
            "preflight_budget_seconds": ready.config.preflight_budget_seconds,
        }
        if ready.audio_enabled
        else {}
    )
    with stop_signal_handlers(stop):
        try:
            result = await run_meeting_session(
                ready.config,
                SessionWiring(
                    stop=stop,
                    open_audio=run.audio_factory,
                    initial_speech=ready.options.initial_speech,
                    conversation=run.conversation,
                    faults=run.faults,
                    journal=run.journal,
                    voice_activity=run.voice_activity,
                    meet_dispatcher=run.meet_dispatcher,
                    visual_store=run.visual_store,
                    visual_captioner=run.visual_captioner,
                    visual_on_caption=run.visual_on_caption,
                    **preflight_kwargs,
                ),
            )
        except FakeYouError as error:
            run.pending_error = error
            result = SessionResult(
                "ended",
                (
                    "infra_failure"
                    if error.exit_code is ExitCode.INFRA_FAILURE
                    else "browser_failure"
                ),
                str(error),
                run.faults.report(),
            )
    run.session_result = result
    return result


def _write_final_ingestion_status(ready: _JoinReady, run: _JoinRun) -> None:
    if (
        ready.profile is None
        or not ready.profile.ingest_transcripts
        or run.record_error is not None
    ):
        return
    try:
        digest = hash_meeting_record(ready.record_path)
        finalized = load_meeting_record(ready.record_path)
        window_count = len(
            meeting_windows(
                finalized,
                window_utterances=ready.settings.memory.ingestion.window_utterances,
                window_seconds=ready.settings.memory.ingestion.window_seconds,
            )
        )
        write_status(
            status_path(ready.record_path),
            IngestionStatus(
                meeting_id=ready.config.meeting_id,
                group_id=ready.profile.memory_group,
                status="pending",
                windows=window_count,
                record_sha256=digest,
                updated_at=datetime.now(UTC),
            ),
        )
    except (OSError, ValueError) as error:
        run.faults.record(classify_exception("meeting_record", error))
        run.record_error = f"ingestion status not written: {type(error).__name__}"


def _finalize_join_record(ready: _JoinReady, run: _JoinRun, result: SessionResult) -> None:
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
    _write_final_ingestion_status(ready, run)
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


def _emit_join_result(ready: _JoinReady, run: _JoinRun, result: SessionResult) -> None:
    resume_command = (
        shlex.join(
            memory_ingest_args(
                meeting_id=ready.config.meeting_id,
                persona=ready.options.identity.persona_path,
                owner_id=ready.config.owner_id,
                twin_id=ready.config.twin_id,
            )
        )
        if run.ingestion_outcome == "pending" and ready.options.identity.persona_path is not None
        else None
    )
    emit_json(
        {
            "meeting_id": ready.config.meeting_id,
            "phase": result.phase,
            "terminal_reason": result.terminal_reason,
            "detail": result.detail,
            "error_code": run.pending_error.code.value if run.pending_error is not None else None,
            "faults": result.faults,
            "session_outcome": "completed" if run.exit_code == 0 else "failed",
            "ingestion_outcome": run.ingestion_outcome,
            "record_error": run.record_error,
            "resume_command": resume_command,
            "brief_sha256": ready.meeting_context.brief_sha256
            if ready.meeting_context is not None
            else None,
            "context_path": str(context_path(ready.record_path))
            if ready.meeting_context is not None
            else None,
        }
    )
    if run.final_exit != 0:
        raise SystemExit(run.final_exit)


async def join_meeting(link: str, *, options: JoinOptions, settings: Settings) -> None:
    audio_enabled = _validate_join_request(link, options, settings)
    config = _build_join_config(link, options, settings)
    profile, brief, meeting_context, config = await _load_join_context(options, settings, config)
    config, record_path = _prepare_session_paths(config, settings)
    ready = _JoinReady(
        options=options,
        settings=settings,
        config=config,
        record_path=record_path,
        profile=profile,
        brief=brief,
        meeting_context=meeting_context,
        audio_enabled=audio_enabled,
    )
    stop = asyncio.Event()
    with ExitStack() as guards:
        if options.profile_dir:
            guards.enter_context(profile_lock(Path(options.profile_dir)))
        with record_lock(record_path, purpose="meeting"):
            resuming, context_reused = await _guard_join_session(ready)
            async with AsyncExitStack() as resources:
                run = _open_join_resources(
                    ready, resources, resuming=resuming, context_reused=context_reused
                )
                await _open_join_memory(ready, run, resources)
                registry = _build_join_layers(ready, run, resources)
                await _open_join_conversation(ready, run, resources)
                _build_audio_factory(ready, run, resources, registry)
                result = await _drive_join_session(ready, run, stop)
                _finalize_join_record(ready, run, result)
    _emit_join_result(ready, run, result)


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
        raise FakeYouError(ErrorCode.MEETING_BRIEF_UNREADABLE, reason=error.strerror) from None
    except ValidationError as error:
        kinds = sorted(
            {item["type"] for item in error.errors(include_url=False, include_input=False)}
        )
        raise FakeYouError(
            ErrorCode.MEETING_BRIEF_SCHEMA_INVALID,
            count=error.error_count(),
            kinds=", ".join(kinds),
        ) from None


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
        raise FakeYouError(ErrorCode.MEETING_CONTEXT_UNREADABLE, path=path) from None
    if context is None:
        if existing is not None:
            raise FakeYouError(ErrorCode.MEETING_STARTED_WITH_BRIEF, meeting_id=config.meeting_id)
        return False
    if existing is not None:
        if existing != context:
            raise FakeYouError(ErrorCode.MEETING_CONTEXT_DIFFERS)
        return True
    thread_id = conversation_thread_id(config.owner_id, config.twin_id, config.meeting_id)
    if await _has_conversation(record_path, thread_id, url=config.database_url):
        raise FakeYouError(
            ErrorCode.MEETING_HAS_CONVERSATION_WITHOUT_CONTEXT, meeting_id=config.meeting_id
        )
    try:
        write_meeting_context(path, context)
    except (OSError, ValueError) as error:
        raise FakeYouError(
            ErrorCode.MEETING_CONTEXT_NOT_WRITTEN, reason=type(error).__name__
        ) from None
    return False


async def _has_conversation(record_path: Path, thread_id: str, url: str | None) -> bool:
    if await asyncio.to_thread(_has_entries, record_path):
        return True
    if url is None:
        return False
    try:
        async with open_checkpointer(url) as saver:
            return await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except FakeYouError as error:
        raise FakeYouError(
            ErrorCode.MEETING_CONVERSATION_CHECKPOINT_UNCHECKABLE, reason=str(error)
        ) from None


def _has_entries(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


async def _has_live_thread(config: SessionConfig) -> bool:
    if config.database_url is None:
        return False
    try:
        async with open_checkpointer(config.database_url) as saver:
            saved = await saver.aget_tuple(
                {
                    "configurable": {
                        "thread_id": meeting_thread_id(config.twin_id, config.meeting_id)
                    }
                }
            )
    except FakeYouError as error:
        raise FakeYouError(
            ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE, reason=str(error)
        ) from None
    return saved is not None and saved.checkpoint["channel_values"].get("phase") != "ended"


def _resume_attempt(record_path: Path) -> int:
    try:
        entries = load_meeting_record(record_path)
    except (OSError, ValueError):
        return 2
    return sum(isinstance(entry, ResumeEntry) for entry in entries) + 2
