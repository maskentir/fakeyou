from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from fakeyou.audio.voice_activity import SlotBinder, VoiceActivityTracker, voice_participant_id
from fakeyou.conversation.conversation import ConversationLoop
from fakeyou.foundation.timeparse import assume_utc, require_iso_datetime
from fakeyou.foundation.transcript import (
    CaptureContext,
    TranscriptEvent,
    TranscriptUpdate,
    merge_transcripts,
    transcript_event,
    transcript_stamp,
)
from fakeyou.identity.identity import SpeakerRegistry
from fakeyou.identity.verify import VerificationInputs, claim_of
from fakeyou.identity.voiceprint_match import (
    VoiceScoreAttribution,
    apply_voice_scores,
    score_turn_audio_async,
)
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import (
    BUG_ERRORS,
    ConsumerSink,
    FaultReporter,
    record_consumer_fault,
)
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.assembly import VoiceprintLive
from fakeyou.persistence.record import MeetingRecord, VisualDraft
from fakeyou.transcription.contract import TranscriptSink
from fakeyou.visual.visual_caption import VisualCaption, frame_ref
from fakeyou.visual.visual_store import VisualFrame, VisualStore, visual_refs_for_utterance


def _stream_slot(stream_id: str) -> int | None:
    raw = stream_id.removeprefix("slot-")
    if raw == stream_id or not raw.isdigit():
        return None
    return int(raw)


def attribute_stream_frame(
    update: TranscriptUpdate, binder: SlotBinder | None
) -> tuple[str, str | None, str, dict[str, Any], float, str | None]:
    capture_ms = update.capture_ms or 0.0
    stream_id = update.stream_id
    slot = _stream_slot(stream_id)
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


async def _emit_transcript(
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
        voice_clusters=list(event.voice_clusters),
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
    record: MeetingRecord,
    update: VisualFrame,
    caption: VisualCaption,
    *,
    journal: Journal | None = None,
) -> None:
    try:
        captured_at = assume_utc(require_iso_datetime(update.captured_at))
    except ValueError as error:
        if journal is not None:
            journal.record(
                "visual_caption_dropped",
                source="meeting_record",
                visual_id=update.frame_id,
                error_type=type(error).__name__,
            )
        return
    record.add_visual(
        VisualDraft(
            visual_id=update.frame_id,
            caption=caption.text,
            sha256=update.sha256,
            captured_at=captured_at,
            frame_ref=frame_ref(update),
            captured_page_ms=update.captured_page_ms,
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

    async def __call__(self, update: TranscriptUpdate) -> None:
        if self._per_stream_enabled:
            await self._per_stream(update)
        else:
            await self._mixed_stream(update)

    def _registry(self) -> SpeakerRegistry | None:
        return self._scoring.registry if self._scoring is not None else None

    async def _score_turn(
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
            scores = await score_turn_audio_async(
                voiceprint.tap_for(stream_id).take_clip(),
                voiceprint.embedder,
                voiceprint.matcher,
                min_seconds=voiceprint.min_seconds,
                timeout_seconds=voiceprint.timeout_seconds,
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
                    slot=_stream_slot(stream_id),
                    embedder=voiceprint.embedder,
                ),
            )
        except TimeoutError as error:
            record_consumer_fault(
                ConsumerSink(self._sinks.faults, self._sinks.journal),
                source="audio",
                detail="voiceprint_scoring_timeout",
                error=error,
            )
        except Exception as error:
            record_consumer_fault(
                ConsumerSink(self._sinks.faults, self._sinks.journal),
                source="audio",
                detail="voiceprint_scoring",
                error=error,
            )
            if isinstance(error, BUG_ERRORS):
                raise

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
            await _emit_transcript(
                ordered,
                journal=self._sinks.journal,
                record=self._sinks.record,
                conversation=self._sinks.conversation,
                extra=self._extras.pop(ordered.id, None),
            )
        self._pending[:] = [event for event in self._pending if event.id not in self._handled]

    async def _per_stream(self, update: TranscriptUpdate) -> None:
        participant_id, evidence_id, stream_id, extra, capture_ms, device = attribute_stream_frame(
            update, self._binder
        )
        event = _visual_event(
            transcript_event(
                update,
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
        await self._score_turn(extra.get("speaker_uuid"), stream_id, device, finalized=update.final)

    async def _mixed_stream(self, update: TranscriptUpdate) -> None:
        voice_activity = self._voice_activity
        attribution = voice_activity.attribute() if voice_activity is not None else None
        speaker_uuid = attribution.speaker_uuid if attribution is not None else None
        display_claim = _claim_for_uuid(self._registry(), speaker_uuid)
        if attribution is None:
            event = transcript_event(
                update, "unknown", None, CaptureContext(display_claim=display_claim)
            )
        else:
            event = transcript_event(
                update,
                attribution.participant_id,
                attribution.evidence_id,
                CaptureContext(display_claim=display_claim),
            )
        await _emit_transcript(
            _visual_event(event, self._sinks.visual_store),
            journal=self._sinks.journal,
            record=self._sinks.record,
            conversation=self._sinks.conversation,
            extra=attribution.journal_fields() if attribution is not None else None,
        )
        await self._score_turn(speaker_uuid, "", None, finalized=update.final)


def build_transcript_callback(
    *,
    sinks: TranscriptSinks,
    binder: SlotBinder,
    voice_activity: VoiceActivityTracker | None,
    scoring: LiveScoring | None = None,
    per_stream_enabled: bool = False,
) -> TranscriptSink:
    return TranscriptDrain(
        sinks=sinks,
        binder=binder,
        voice_activity=voice_activity,
        scoring=scoring,
        per_stream_enabled=per_stream_enabled,
    )
