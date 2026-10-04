import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from fakeyou.assembly import stop_signal_handlers
from fakeyou.brief import MeetingContext, context_path, validate_brief_scope
from fakeyou.cli.errors import CliError
from fakeyou.cli.memory import load_operator_persona
from fakeyou.debrief import (
    debrief_report_status,
    generate_debrief,
    read_debrief,
    rebuild_debrief_markdown,
    write_debrief,
)
from fakeyou.debrief_schema import DebriefReport, EvidenceSource
from fakeyou.debrief_sources import chunk_sources, load_context_snapshot, load_record_snapshot
from fakeyou.ingestion_status import (
    RecordLockError,
    read_status,
    record_lock,
    status_path,
    write_atomically,
)
from fakeyou.invariants import SessionId
from fakeyou.journal import Journal, journal_path
from fakeyou.persona import PersonaProfile
from fakeyou.providers import create_debrief_chat_model, create_fallback_chat_model
from fakeyou.record import meeting_record_path
from fakeyou.settings import Settings


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
            raise CliError(
                "record ownership cannot be verified without context or ingestion status"
            )
    elif status.meeting_id != meeting_id or status.group_id != profile.memory_group:
        raise CliError("meeting record scope does not match the operator persona")


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
        raise CliError("no meeting record is available for debrief")
    try:
        with record_lock(record_path, purpose="debrief"):
            await _debrief_locked(record_path, profile, meeting_id, force, settings)
    except RecordLockError:
        raise CliError("meeting record is locked by another operation") from None


async def _debrief_locked(
    record_path: Path, profile: PersonaProfile, meeting_id: str, force: bool, settings: Settings
) -> None:
    journal = Journal(
        meeting_id,
        path=journal_path(settings.paths.journal_dir, meeting_id),
        stdout=False,
    )
    record_digest: str | None = None
    context_digest: str | None = None
    started = time.monotonic()
    try:
        snapshot = load_record_snapshot(record_path)
        record_digest = snapshot.record_sha256
        context, context_digest = load_context_snapshot(context_path(record_path))
        _validate_scope(record_path, context, profile, meeting_id)
        chunks = chunk_sources(snapshot.sources)
        if any(record_path.parent.glob("debrief.json.*.rollback")):
            raise CliError("canonical debrief requires publication recovery")
        if not force and debrief_report_status(record_path) == "available":
            report = read_debrief(record_path)
            if (report.owner_id, report.twin_id, report.meeting_id) != (
                profile.owner_id,
                profile.twin_id,
                meeting_id,
            ):
                raise CliError("cached debrief scope does not match the operator persona")
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
        current = load_record_snapshot(record_path)
        _, current_context_digest = load_context_snapshot(context_path(record_path))
        if current.record_sha256 != record_digest or current_context_digest != context_digest:
            raise CliError("meeting sources changed during debrief generation")
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
        if any(entry.participant_id == "unknown" for entry in snapshot.sources):
            limitations.append(
                "Unknown participants have not been identified or mapped to the owner."
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
        if isinstance(error, CliError):
            raise
        raise CliError(f"debrief failed: {type(error).__name__}") from None
    finally:
        journal.close()


def _print_result(record_path: Path, *, cached: bool) -> None:
    print(
        json.dumps(
            {
                "meeting_id": record_path.parent.name,
                "status": "completed",
                "review_status": "unreviewed",
                "cached": cached,
                "json_path": str(record_path.parent / "debrief.json"),
                "markdown_path": str(record_path.parent / "debrief.md"),
            },
            indent=2,
        )
    )
