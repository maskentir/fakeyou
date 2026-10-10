import asyncio
import os
import shlex
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from graphiti_core import Graphiti
from pipecat.frames.frames import TranscriptionFrame
from pydantic import ValidationError

from fakeyou.audio.audio import open_meeting_audio
from fakeyou.audio.playback import PlaybackEvent
from fakeyou.audio.stream_capture import STREAM_FLEET_SLOTS, open_stream_fleet
from fakeyou.audio.voice_activity import SlotBinder, VoiceActivityTracker, voice_participant_id
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.cli._operator import load_operator_persona
from fakeyou.cli.output import emit_json
from fakeyou.config.meeting_request import validate_meeting_request
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
    TranscriptEvent,
    merge_transcripts,
    transcript_event,
    transcript_stamp,
)
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import claim_of
from fakeyou.identity.voiceprint_match import apply_voice_scores, score_turn_audio
from fakeyou.meet.browser import profile_lock
from fakeyou.meet.participant_observer import EVIDENCE_FILE_NAME
from fakeyou.memory.ingestion import meeting_windows
from fakeyou.memory.persona import (
    PersonaProfile,
    meeting_tool_names,
    persona_digest,
    speech_keywords,
)
from fakeyou.observability.faults import FaultReporter, record_consumer_fault
from fakeyou.observability.journal import Journal, journal_path
from fakeyou.observability.provider_faults import ProviderFault, classify_exception
from fakeyou.observability.report import describe_session
from fakeyou.orchestration.assembly import (
    VoiceprintLive,
    create_visual_captioner,
    open_conversation,
    open_voiceprint,
    stop_signal_handlers,
)
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.orchestration.providers import memory_config, open_persona_memory, preflight_hosts
from fakeyou.orchestration.runtime import MeetActionDispatcher, run_meeting_session
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
    load_meeting_record,
    meeting_record_path,
)
from fakeyou.visual.visual_caption import VisualCaption, frame_ref
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


def _score_live_turn(
    voiceprint: VoiceprintLive,
    registry: SpeakerRegistry,
    speaker_uuid: str,
    stream_id: str,
    device: str | None,
    journal: Journal,
    faults: FaultReporter | None,
) -> None:
    try:
        scores = score_turn_audio(
            voiceprint.tap_for(stream_id).take_clip(),
            voiceprint.embedder,
            voiceprint.matcher,
            min_seconds=voiceprint.min_seconds,
        )
        if scores is None:
            return
        row = registry.row(speaker_uuid)
        claim = claim_of(row) if row is not None else None
        apply_voice_scores(
            registry,
            speaker_uuid,
            scores,
            device=device,
            owners=voiceprint.owners,
            journal=journal,
            faults=faults,
            artifact=voiceprint.artifact,
            claim=claim,
            slot=stream_slot(stream_id),
            embedder=voiceprint.embedder,
        )
    except Exception as error:
        record_consumer_fault(
            faults, journal, source="audio", detail="voiceprint_scoring", error=error
        )


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
        visual_id=frame.frame_id,
        caption=caption.text,
        sha256=frame.sha256,
        captured_at=captured_at,
        frame_ref=frame_ref(frame),
        captured_page_ms=frame.captured_page_ms,
    )


def _claim_for_uuid(registry: SpeakerRegistry | None, speaker_uuid: Any) -> str | None:
    if registry is None or not isinstance(speaker_uuid, str) or not speaker_uuid:
        return None
    row = registry.row(speaker_uuid)
    if row is None:
        return None
    return claim_of(row)


def build_transcript_callback(
    *,
    journal: Journal,
    record: MeetingRecord,
    conversation: ConversationLoop | None,
    voice_activity: VoiceActivityTracker | None,
    binder: SlotBinder,
    per_stream_enabled: bool = False,
    voiceprint: VoiceprintLive | None = None,
    registry: SpeakerRegistry | None = None,
    faults: FaultReporter | None = None,
    visual_store: VisualStore | None = None,
) -> Callable[[TranscriptionFrame], Awaitable[None]]:
    pending: list[TranscriptEvent] = []
    handled: set[str] = set()
    extras: dict[str, dict[str, Any]] = {}
    emitted_max: TranscriptEvent | None = None

    async def transcript(frame: TranscriptionFrame) -> None:
        nonlocal emitted_max
        if per_stream_enabled:
            participant_id, evidence_id, stream_id, extra, capture_ms, device = (
                attribute_stream_frame(frame, binder)
            )
            event = transcript_event(
                frame,
                participant_id,
                evidence_id,
                stream_id,
                capture_ms=capture_ms,
                display_claim=_claim_for_uuid(registry, extra.get("speaker_uuid")),
            )
            if visual_store is not None:
                event = replace(
                    event,
                    visual_refs=visual_refs_for_utterance(
                        visual_store.frames,
                        capture_ms=event.capture_ms,
                        spoken_at=event.timestamp,
                    ),
                )
            pending.append(event)
            extras[event.id] = extra
            for ordered in merge_transcripts(pending):
                if ordered.id in handled:
                    continue
                handled.add(ordered.id)
                if emitted_max is not None and transcript_stamp(ordered) < transcript_stamp(
                    emitted_max
                ):
                    journal.record(
                        "order_inversion_detected",
                        source="stt",
                        transcript_id=ordered.id,
                        spoken_at=ordered.timestamp,
                        emitted_max_spoken_at=emitted_max.timestamp,
                        stream_id=ordered.stream_id,
                    )
                if emitted_max is None or transcript_stamp(emitted_max) < transcript_stamp(ordered):
                    emitted_max = ordered
                await emit_transcript(
                    ordered,
                    journal=journal,
                    record=record,
                    conversation=conversation,
                    extra=extras.pop(ordered.id, None),
                )
            pending[:] = [event for event in pending if event.id not in handled]
            speaker_uuid = extra.get("speaker_uuid")
            if voiceprint is not None and registry is not None and frame.finalized:
                if isinstance(speaker_uuid, str) and speaker_uuid:
                    _score_live_turn(
                        voiceprint, registry, speaker_uuid, stream_id, device, journal, faults
                    )
                else:
                    voiceprint.tap_for(stream_id).take_clip()
        else:
            attribution = voice_activity.attribute() if voice_activity is not None else None
            legacy_uuid = attribution.speaker_uuid if attribution is not None else None
            legacy_claim = _claim_for_uuid(registry, legacy_uuid)
            event = (
                transcript_event(frame, "unknown", None, "", display_claim=legacy_claim)
                if attribution is None
                else transcript_event(
                    frame,
                    attribution.participant_id,
                    attribution.evidence_id,
                    "",
                    display_claim=legacy_claim,
                )
            )
            if visual_store is not None:
                event = replace(
                    event,
                    visual_refs=visual_refs_for_utterance(
                        visual_store.frames,
                        capture_ms=event.capture_ms,
                        spoken_at=event.timestamp,
                    ),
                )
            await emit_transcript(
                event,
                journal=journal,
                record=record,
                conversation=conversation,
                extra=attribution.journal_fields() if attribution is not None else None,
            )
            if voiceprint is not None and registry is not None and frame.finalized:
                if attribution is not None and attribution.speaker_uuid:
                    _score_live_turn(
                        voiceprint, registry, attribution.speaker_uuid, "", None, journal, faults
                    )
                else:
                    voiceprint.tap_for("").take_clip()

    return transcript


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
    validate_meeting_request(
        link=link,
        lobby_timeout=lobby_timeout,
        duration=duration,
        initial_speech=initial_speech,
        audio=audio_enabled,
        conversation=conversation_enabled,
        memory=memory,
        persona_path=persona_path,
        brief_path=brief_path,
        meeting_id=meeting_id,
        wake_phrase=wake_phrase,
        barge_in=barge_in,
    )
    if settings.conversation.participant_policy_enabled and not (wake_phrase or "").strip():
        raise FakeYouError(ErrorCode.MEETING_PARTICIPANT_POLICY_REQUIRES_ALIASES)
    if conversation_enabled:
        settings.require_roles("reply", "reply_fallback")
    if memory:
        memory_config(settings)
    audio_factory: Callable[[], AbstractAsyncContextManager[MeetingAudio]] | None = None
    if audio_enabled:
        if (
            sys.platform != "linux"
            or os.environ.get("PULSE_SOURCE") != MEETING_MONITOR_SOURCE_NAME
            or os.environ.get("PULSE_SINK") != AGENT_SINK_NAME
            or find_spec("pyaudio") is None
        ):
            raise FakeYouError(ErrorCode.MEETING_AUDIO_REQUIRES_WORKER)
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
            raise FakeYouError(ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH)
        if memory and not (profile.allowed_tools or profile.ingest_transcripts):
            raise FakeYouError(ErrorCode.MEETING_MEMORY_REQUIRES_RECALL_OR_INGEST)
        if brief_path is not None:
            brief = await _load_brief(brief_path, config)
            meeting_tool_names(profile, brief, memory_requested=memory)
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
        per_stream_enabled=settings.conversation.per_stream_enabled,
        meet_actions_enabled=settings.conversation.meet_actions_enabled,
        visual_enabled=settings.visual.enabled,
    )
    ingest_requested = profile is not None and profile.ingest_transcripts
    ingestion_outcome = "pending" if ingest_requested else "not_requested"
    record_error: str | None = None
    result = SessionResult("ended", None, "", None)
    exit_code = ExitCode.FAILURE
    final_exit = ExitCode.FAILURE
    with ExitStack() as guards:
        if profile_dir:
            guards.enter_context(profile_lock(Path(profile_dir)))
        with record_lock(record_path, purpose="meeting"):
            session_state = describe_session(settings.paths.journal_dir, resolved_meeting_id)
            if session_state == "unfinished" and not resume:
                raise FakeYouError(
                    ErrorCode.MEETING_UNFINISHED_SESSION, meeting_id=resolved_meeting_id
                )
            if session_state == "finished" and resume:
                raise FakeYouError(ErrorCode.MEETING_ALREADY_ENDED, meeting_id=resolved_meeting_id)
            resuming = resume and (session_state == "unfinished" or await _has_live_thread(config))
            context_reused = await _settle_meeting_context(record_path, config, meeting_context)
            conversation: ConversationLoop | None = None
            graphiti: Graphiti | None = None
            session_result: SessionResult | None = None
            pending_error: FakeYouError | None = None
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
                            str(FakeYouError(ErrorCode.MEMORY_UNAVAILABLE, reason=fault)),
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

                speaker_registry = SpeakerRegistry(capacity=STREAM_FLEET_SLOTS)
                voiceprint_live = resources.enter_context(open_voiceprint(settings))
                voice_activity = (
                    VoiceActivityTracker(twin_name=config.twin_name, registry=speaker_registry)
                    if settings.conversation.voice_labels_enabled and audio_enabled
                    else None
                )
                meet_dispatcher = (
                    MeetActionDispatcher(
                        record_path,
                        meeting_id=resolved_meeting_id,
                        enabled=settings.conversation.meet_actions_enabled,
                        journal=journal,
                    )
                    if settings.conversation.meet_actions_enabled and conversation_enabled
                    else None
                )
                visual_store = (
                    VisualStore(record_path.parent / "visual", journal=journal)
                    if settings.visual.enabled
                    else None
                )
                visual_captioner = (
                    create_visual_captioner(settings, journal=journal)
                    if visual_store is not None and conversation_enabled
                    else None
                )
                visual_on_caption = (
                    partial(record_visual_caption, record)
                    if visual_store is not None and visual_captioner is not None
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
                            meet_dispatcher=meet_dispatcher,
                            visual_store=visual_store,
                            visual_captioner=visual_captioner,
                        )
                    )

                stream_binder = SlotBinder(capacity=STREAM_FLEET_SLOTS, registry=speaker_registry)
                transcript = build_transcript_callback(
                    journal=journal,
                    record=record,
                    conversation=conversation,
                    voice_activity=voice_activity,
                    binder=stream_binder,
                    per_stream_enabled=settings.conversation.per_stream_enabled,
                    voiceprint=voiceprint_live,
                    registry=speaker_registry,
                    faults=faults,
                    visual_store=visual_store,
                )

                async def interim(frame: TranscriptionFrame) -> None:
                    if conversation is not None:
                        await conversation.handle_interim(frame.text)

                async def vad(active: bool) -> None:
                    if conversation is not None:
                        await conversation.note_vad(active)

                audio_recorder: SlotAudioWriter | None = None
                if settings.audio.persist_enabled and settings.conversation.per_stream_enabled:
                    audio_recorder = SlotAudioWriter(record_path.parent / "audio", journal=journal)
                    resources.callback(audio_recorder.close)
                if audio_enabled:
                    audio_probe = (
                        AttributionProbe(config.attribution_probe_path)
                        if config.attribution_probe_path is not None
                        else None
                    )
                    stt_keywords = speech_keywords(profile, config.twin_name) if profile else None
                    if settings.conversation.per_stream_enabled:
                        audio_factory = partial(
                            open_stream_fleet,
                            slots=STREAM_FLEET_SLOTS,
                            binder=stream_binder,
                            tts_settings=settings.tts,
                            speech_language=settings.speech.language,
                            stt_settings=settings.stt,
                            alone_leave_seconds=settings.audio_health.alone_leave_seconds,
                            on_transcript=transcript,
                            on_interim=interim,
                            on_vad=vad,
                            on_playback_event=log_playback,
                            faults=faults,
                            journal=journal,
                            recorder=audio_recorder,
                            deaf_window_seconds=config.deaf_window_seconds,
                            deaf_grace_seconds=config.deaf_grace_seconds,
                            stt_keywords=stt_keywords,
                            turn_tap_factory=(
                                voiceprint_live.tap_for_slot
                                if voiceprint_live is not None
                                else None
                            ),
                        )
                    else:
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
                            on_vad=vad,
                            on_playback_event=log_playback,
                            faults=faults,
                            journal=journal,
                            deaf_window_seconds=config.deaf_window_seconds,
                            deaf_grace_seconds=config.deaf_grace_seconds,
                            stt_keywords=stt_keywords,
                            turn_tap=voiceprint_live.tap if voiceprint_live is not None else None,
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
                        try:
                            session_result = await run_meeting_session(
                                config,
                                stop=stop,
                                open_audio=audio_factory,
                                initial_speech=initial_speech,
                                conversation=conversation,
                                faults=faults,
                                journal=journal,
                                voice_activity=voice_activity,
                                meet_dispatcher=meet_dispatcher,
                                visual_store=visual_store,
                                visual_captioner=visual_captioner,
                                visual_on_caption=visual_on_caption,
                                **preflight_kwargs,
                            )
                        except FakeYouError as error:
                            pending_error = error
                            session_result = SessionResult(
                                "ended",
                                (
                                    "infra_failure"
                                    if error.exit_code is ExitCode.INFRA_FAILURE
                                    else "browser_failure"
                                ),
                                str(error),
                                faults.report(),
                            )
                result = session_result
                exit_code = (
                    ExitCode.FAILURE
                    if result.terminal_reason is None
                    else TERMINAL_EXIT_CODES[result.terminal_reason]
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
                    final_exit = ExitCode.FAILURE
                if pending_error is not None:
                    final_exit = pending_error.exit_code
                journal.record(
                    "cli_exited",
                    source="cli",
                    terminal_reason=result.terminal_reason,
                    exit_code=final_exit,
                )
    resume_command = (
        shlex.join(
            memory_ingest_args(
                meeting_id=resolved_meeting_id,
                persona=persona_path,
                owner_id=config.owner_id,
                twin_id=config.twin_id,
            )
        )
        if ingestion_outcome == "pending" and persona_path is not None
        else None
    )
    emit_json(
        {
            "meeting_id": resolved_meeting_id,
            "phase": result.phase,
            "terminal_reason": result.terminal_reason,
            "detail": result.detail,
            "error_code": pending_error.code.value if pending_error is not None else None,
            "faults": result.faults,
            "session_outcome": "completed" if exit_code == 0 else "failed",
            "ingestion_outcome": ingestion_outcome,
            "record_error": record_error,
            "resume_command": resume_command,
            "brief_sha256": meeting_context.brief_sha256 if meeting_context is not None else None,
            "context_path": str(context_path(record_path)) if meeting_context is not None else None,
        }
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
