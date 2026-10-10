import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from fakeyou.audio.voice_activity import voice_speaker_name
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli._operator import load_operator_persona
from fakeyou.cli.output import emit_json
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import (
    debrief_report_status,
    generate_debrief,
    read_debrief,
    rebuild_debrief_markdown,
    write_debrief,
)
from fakeyou.debrief.debrief_schema import DebriefReport, EvidenceSource
from fakeyou.debrief.debrief_sources import (
    chunk_sources,
    load_context_snapshot,
    load_record_snapshot,
)
from fakeyou.diarization.correction_labels import describe_correction_speakers
from fakeyou.diarization.diarize_correction import correction_digest
from fakeyou.foundation.invariants import SessionId
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.journal import Journal, journal_path
from fakeyou.orchestration.assembly import stop_signal_handlers
from fakeyou.orchestration.providers import create_debrief_chat_model, create_fallback_chat_model
from fakeyou.persistence.brief import MeetingContext, context_path, validate_brief_scope
from fakeyou.persistence.ingestion_status import (
    read_status,
    record_lock,
    status_path,
    write_atomically,
)
from fakeyou.persistence.record import meeting_record_path


def debrief_total_budget(settings: Settings, chunk_count: int) -> float:
    roles = [settings.debrief_role]
    if settings.reply_fallback is not None:
        roles.append(settings.reply_fallback)
    return sum(role.timeout_seconds * (role.max_retries + 1) for role in roles) * chunk_count


def _validate_scope(
    record_path: Path, context: MeetingContext | None, profile: PersonaProfile, meeting_id: str
) -> None:
    if context is not None:
        validate_brief_scope(
            context.brief,
            owner_id=profile.owner_id,
            twin_id=profile.twin_id,
            meeting_id=meeting_id,
        )
    status = read_status(status_path(record_path))
    if status is None:
        if context is None:
            raise FakeYouError(ErrorCode.DEBRIEF_OWNERSHIP_UNVERIFIABLE)
    elif status.meeting_id != meeting_id or status.group_id != profile.memory_group:
        raise FakeYouError(ErrorCode.DEBRIEF_SCOPE_MISMATCH)


def _write_attempt(
    record_path: Path,
    state: Literal["failed", "interrupted"],
    *,
    error: str,
    record_sha256: str | None,
    context_sha256: str | None,
    journal: Journal,
) -> None:
    payload = {
        "status": state,
        "error": error,
        "record_sha256": record_sha256,
        "context_sha256": context_sha256,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    try:
        write_atomically(
            record_path.parent / "debrief-attempt.json",
            (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
        )
    except OSError as fault:
        journal.record("debrief_attempt_write_failed", source="cli", error=type(fault).__name__)


async def debrief_meeting(
    meeting_id: str,
    persona_path: Path,
    owner_id: str | None,
    twin_id: str | None,
    force: bool,
    settings: Settings,
) -> None:
    TypeAdapter(SessionId).validate_python(meeting_id)
    profile = await load_operator_persona(persona_path, owner_id, twin_id, settings)
    record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
    if not record_path.is_file():
        raise FakeYouError(ErrorCode.DEBRIEF_RECORD_MISSING)
    with record_lock(record_path, purpose="debrief"):
        await _debrief_locked(record_path, profile, meeting_id, force, settings)


def _is_anonymous(participant_id: str) -> bool:
    if participant_id == "unknown":
        return True
    try:
        parsed = uuid.UUID(participant_id)
    except ValueError:
        return False
    return str(parsed) == participant_id.lower()


async def _debrief_locked(
    record_path: Path, profile: PersonaProfile, meeting_id: str, force: bool, settings: Settings
) -> None:
    journal = Journal(
        meeting_id,
        path=journal_path(settings.paths.journal_dir, meeting_id),
        echo=False,
    )
    record_digest: str | None = None
    context_digest: str | None = None
    sidecar_digest: str | None = None
    started = time.monotonic()
    try:
        snapshot = load_record_snapshot(record_path, journal=journal)
        record_digest = snapshot.record_sha256
        sidecar_digest = correction_digest(record_path)
        context, context_digest = load_context_snapshot(context_path(record_path))
        _validate_scope(record_path, context, profile, meeting_id)
        chunks = chunk_sources(snapshot.sources)
        if any(record_path.parent.glob("debrief.json.*.rollback")):
            raise FakeYouError(ErrorCode.DEBRIEF_PUBLICATION_RECOVERY)
        if not force and debrief_report_status(record_path, journal=journal) == "available":
            report = read_debrief(record_path)
            if (report.owner_id, report.twin_id, report.meeting_id) != (
                profile.owner_id,
                profile.twin_id,
                meeting_id,
            ):
                raise FakeYouError(ErrorCode.DEBRIEF_CACHED_SCOPE_MISMATCH)
            rebuild_debrief_markdown(record_path)
            _print_result(record_path, cached=True)
            return
        model = create_debrief_chat_model(settings)
        fallback = create_fallback_chat_model(settings)
        stop = asyncio.Event()
        journal.record(
            "debrief_started",
            source="cli",
            record_sha256=record_digest,
            context_sha256=context_digest,
            source_count=len(snapshot.sources),
            chunk_count=len(chunks),
        )
        with stop_signal_handlers(stop):
            async with asyncio.timeout(debrief_total_budget(settings, len(chunks))):
                draft = await generate_debrief(
                    model,
                    snapshot,
                    brief=context.brief if context else None,
                    stop=stop,
                    fallback=fallback,
                    journal=journal,
                )
        if stop.is_set():
            raise asyncio.CancelledError
        current = load_record_snapshot(record_path, journal=journal)
        _, current_context_digest = load_context_snapshot(context_path(record_path))
        if (
            current.record_sha256 != record_digest
            or current_context_digest != context_digest
            or correction_digest(record_path) != sidecar_digest
        ):
            raise FakeYouError(ErrorCode.DEBRIEF_SOURCES_CHANGED)
        cited = {reference.entry_id for item in draft.items for reference in item.evidence}
        cited.update(
            reference.entry_id for finding in draft.findings for reference in finding.evidence
        )
        limitations = [
            "Participant statements are claims, not authenticated owner decisions.",
            "Matching quotes do not prove summary entailment; independent review is required.",
            "Completed twin playback is application evidence, not a host hearing confirmation.",
            "Assignees and deadlines remain source claims; no external actions were performed.",
        ]
        if context is None:
            limitations.append(
                "No historical meeting brief is available; objectives are unavailable."
            )
        if any(_is_anonymous(entry.participant_id) for entry in snapshot.sources):
            limitations.append(
                "Unknown participants have not been identified or mapped to the owner."
            )
        voice_labels = []
        for entry in snapshot.sources:
            label = voice_speaker_name(entry.participant_id, entry.display_claim)
            if label is not None and label not in voice_labels:
                voice_labels.append(label)
        if voice_labels:
            limitations.append(
                "Speaker labels are anonymous voice labels within this meeting, "
                "not verified identities: " + "; ".join(voice_labels) + "."
            )
        correction_names = describe_correction_speakers(record_path)
        correction_speakers = []
        for entry in snapshot.sources:
            label = correction_names.get(entry.participant_id)
            if label is not None and label not in correction_speakers:
                correction_speakers.append(label)
        if correction_speakers:
            limitations.append(
                "Correction speakers are anonymous diarization labels within this meeting, "
                "not verified identities: " + "; ".join(correction_speakers) + "."
            )
        if fallback is not None:
            limitations.append(
                "Provider/model identify the configured primary; fallback may have contributed."
            )
        report = DebriefReport(
            schema_version="meeting_debrief/v1",
            owner_id=profile.owner_id,
            twin_id=profile.twin_id,
            meeting_id=meeting_id,
            record_sha256=record_digest,
            context_sha256=context_digest,
            correction_sha256=sidecar_digest,
            generated_at=datetime.now(UTC),
            provider=settings.debrief_role.provider,
            model=settings.debrief_role.model,
            source_count=len(snapshot.sources),
            chunk_count=len(chunks),
            status="completed",
            review_status="unreviewed",
            items=draft.items,
            findings=draft.findings,
            evidence_sources=tuple(
                EvidenceSource(
                    id=entry.id,
                    text=entry.text,
                    kind=entry.kind,
                    participant_id=entry.participant_id,
                    at=entry.at,
                )
                for entry in snapshot.sources
                if entry.id in cited
            ),
            source_limitations=tuple(limitations),
        )
        write_debrief(record_path, report)
        journal.record(
            "debrief_finished",
            source="cli",
            duration_ms=(time.monotonic() - started) * 1000,
            item_count=len(report.items),
            record_sha256=record_digest,
            context_sha256=context_digest,
        )
        _print_result(record_path, cached=False)
    except asyncio.CancelledError:
        _write_attempt(
            record_path,
            "interrupted",
            error="CancelledError",
            record_sha256=record_digest,
            context_sha256=context_digest,
            journal=journal,
        )
        journal.record("debrief_failed", source="cli", error="CancelledError")
        raise
    except Exception as error:
        _write_attempt(
            record_path,
            "failed",
            error=type(error).__name__,
            record_sha256=record_digest,
            context_sha256=context_digest,
            journal=journal,
        )
        journal.record("debrief_failed", source="cli", error=type(error).__name__)
        if isinstance(error, FakeYouError):
            raise
        raise FakeYouError(ErrorCode.DEBRIEF_FAILED, reason=type(error).__name__) from None
    finally:
        journal.close()


def _print_result(record_path: Path, *, cached: bool) -> None:
    emit_json(
        {
            "meeting_id": record_path.parent.name,
            "status": "completed",
            "review_status": "unreviewed",
            "cached": cached,
            "json_path": str(record_path.parent / "debrief.json"),
            "markdown_path": str(record_path.parent / "debrief.md"),
        }
    )
