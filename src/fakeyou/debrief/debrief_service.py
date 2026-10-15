import asyncio
import json
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief import (
    DebriefModels,
    DebriefStopRequested,
    debrief_report_status,
    generate_debrief,
    read_debrief,
    rebuild_debrief_markdown,
    write_debrief,
)
from fakeyou.debrief.debrief_schema import DebriefDraft, DebriefReport, EvidenceSource
from fakeyou.debrief.debrief_sources import (
    RecordSnapshot,
    SourceEntry,
    chunk_sources,
    load_context_snapshot,
    load_record_snapshot,
    voice_speaker_labels,
)
from fakeyou.diarization.correction_labels import describe_correction_speakers
from fakeyou.diarization.diarize_correction import correction_digest
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.persistence.brief import MeetingContext, context_path, validate_brief_scope
from fakeyou.persistence.files import write_atomically
from fakeyou.persistence.ingestion_status import read_status, record_lock, status_path
from fakeyou.persistence.layout import REPORT_FILE_NAME, MeetingDir
from fakeyou.persistence.record import meeting_record_path


class DebriefOperator(Protocol):
    @property
    def owner_id(self) -> SessionId: ...

    @property
    def twin_id(self) -> SessionId: ...

    @property
    def memory_group(self) -> str: ...


class StopSignalBinding(Protocol):
    @property
    def installed(self) -> tuple[str, ...]: ...

    @property
    def unavailable(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class DebriefRequest:
    meeting_id: SessionId
    operator: DebriefOperator
    settings: Settings
    force: bool


@dataclass(frozen=True, slots=True)
class DebriefWiring:
    models: Callable[[], DebriefModels]
    stop_signals: Callable[[asyncio.Event], AbstractContextManager[StopSignalBinding]]


@dataclass(frozen=True, slots=True)
class DebriefOutcome:
    meeting_id: SessionId
    cached: bool
    json_path: Path
    markdown_path: Path


def debrief_total_budget(settings: Settings, chunk_count: int) -> float:
    roles = [settings.debrief_role]
    if settings.reply_fallback is not None:
        roles.append(settings.reply_fallback)
    return sum(role.timeout_seconds * (role.max_retries + 1) for role in roles) * chunk_count


async def run_debrief(request: DebriefRequest, wiring: DebriefWiring) -> DebriefOutcome:
    record_path = meeting_record_path(request.settings.paths.record_dir, request.meeting_id)
    if not record_path.is_file():
        raise FakeYouError(ErrorCode.DEBRIEF_RECORD_MISSING)
    with record_lock(record_path, purpose="debrief"):
        return await _debrief_locked(record_path, request, wiring)


@dataclass(slots=True)
class _Attempt:
    record_path: Path
    request: DebriefRequest
    wiring: DebriefWiring
    journal: Journal
    started: float = field(default_factory=time.monotonic)
    record_sha256: str | None = None
    context_sha256: str | None = None
    correction_sha256: str | None = None

    def fail(self, state: Literal["failed", "interrupted"], error: str) -> None:
        payload = {
            "status": state,
            "error": error,
            "record_sha256": self.record_sha256,
            "context_sha256": self.context_sha256,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            write_atomically(
                MeetingDir.from_record_path(self.record_path).debrief_attempt,
                (json.dumps(payload, indent=2) + "\n").encode("utf-8"),
            )
        except OSError as fault:
            self.journal.record(
                "debrief_attempt_write_failed", source="cli", error=type(fault).__name__
            )
        self.journal.record("debrief_failed", source="cli", error=error)


async def _debrief_locked(
    record_path: Path, request: DebriefRequest, wiring: DebriefWiring
) -> DebriefOutcome:
    journal = Journal(
        request.meeting_id,
        file=JournalFile(journal_path(request.settings.paths.journal_dir, request.meeting_id)),
        echo=False,
    )
    attempt = _Attempt(record_path, request, wiring, journal)
    try:
        return await _publish(attempt)
    except DebriefStopRequested:
        attempt.fail("interrupted", "DebriefStopRequested")
        raise FakeYouError(ErrorCode.OPERATION_INTERRUPTED) from None
    except asyncio.CancelledError:
        attempt.fail("interrupted", "CancelledError")
        raise
    except Exception as error:
        attempt.fail("failed", type(error).__name__)
        if isinstance(error, (FakeYouError, ProviderFault)):
            raise
        raise FakeYouError(ErrorCode.DEBRIEF_FAILED, reason=type(error).__name__) from error
    finally:
        journal.close()


async def _publish(attempt: _Attempt) -> DebriefOutcome:
    request = attempt.request
    record_path = attempt.record_path
    snapshot = load_record_snapshot(record_path, journal=attempt.journal)
    attempt.record_sha256 = snapshot.record_sha256
    attempt.correction_sha256 = correction_digest(record_path)
    context, attempt.context_sha256 = load_context_snapshot(context_path(record_path))
    _validate_scope(
        record_path, context, request.operator, request.meeting_id, journal=attempt.journal
    )
    chunks = chunk_sources(snapshot.sources)
    if any(MeetingDir.from_record_path(record_path).dir.glob(f"{REPORT_FILE_NAME}.*.rollback")):
        raise FakeYouError(ErrorCode.DEBRIEF_PUBLICATION_RECOVERY)
    cached = _serve_cached(attempt)
    if cached is not None:
        return cached
    models = attempt.wiring.models()
    draft = await _generate(attempt, models, snapshot, context, len(chunks))
    _ensure_sources_fresh(attempt)
    limitations = _source_limitations(
        snapshot, record_path, context, fallback_used=models.fallback is not None
    )
    report = _build_report(
        attempt, snapshot, draft, chunk_count=len(chunks), limitations=limitations
    )
    write_debrief(record_path, report)
    attempt.journal.record(
        "debrief_finished",
        source="cli",
        duration_ms=(time.monotonic() - attempt.started) * 1000,
        item_count=len(report.items),
        record_sha256=attempt.record_sha256,
        context_sha256=attempt.context_sha256,
    )
    return _outcome(record_path, cached=False)


def _validate_scope(
    record_path: Path,
    context: MeetingContext | None,
    operator: DebriefOperator,
    meeting_id: SessionId,
    *,
    journal: Journal | None = None,
) -> None:
    if context is not None:
        validate_brief_scope(
            context.brief,
            owner_id=operator.owner_id,
            twin_id=operator.twin_id,
            meeting_id=meeting_id,
        )
    status = read_status(status_path(record_path), journal=journal)
    if status is None:
        if context is None:
            raise FakeYouError(ErrorCode.DEBRIEF_OWNERSHIP_UNVERIFIABLE)
    elif status.meeting_id != meeting_id or status.group_id != operator.memory_group:
        raise FakeYouError(ErrorCode.DEBRIEF_SCOPE_MISMATCH)


def _serve_cached(attempt: _Attempt) -> DebriefOutcome | None:
    request = attempt.request
    if request.force:
        return None
    if debrief_report_status(attempt.record_path, journal=attempt.journal) != "available":
        return None
    report = read_debrief(attempt.record_path)
    if (report.owner_id, report.twin_id, report.meeting_id) != (
        request.operator.owner_id,
        request.operator.twin_id,
        request.meeting_id,
    ):
        raise FakeYouError(ErrorCode.DEBRIEF_CACHED_SCOPE_MISMATCH)
    rebuild_debrief_markdown(attempt.record_path)
    return _outcome(attempt.record_path, cached=True)


async def _generate(
    attempt: _Attempt,
    models: DebriefModels,
    snapshot: RecordSnapshot,
    context: MeetingContext | None,
    chunk_count: int,
) -> DebriefDraft:
    stop = asyncio.Event()
    attempt.journal.record(
        "debrief_started",
        source="cli",
        record_sha256=attempt.record_sha256,
        context_sha256=attempt.context_sha256,
        source_count=len(snapshot.sources),
        chunk_count=chunk_count,
    )
    with attempt.wiring.stop_signals(stop) as signals:
        attempt.journal.record(
            "signal_handlers_bound",
            source="cli",
            signals=list(signals.installed),
            unavailable=list(signals.unavailable),
        )
        async with asyncio.timeout(debrief_total_budget(attempt.request.settings, chunk_count)):
            draft = await generate_debrief(
                models,
                snapshot,
                brief=context.brief if context else None,
                stop=stop,
                journal=attempt.journal,
            )
    if stop.is_set():
        raise DebriefStopRequested
    return draft


def _ensure_sources_fresh(attempt: _Attempt) -> None:
    current = load_record_snapshot(attempt.record_path, journal=attempt.journal)
    _, current_context_digest = load_context_snapshot(context_path(attempt.record_path))
    if (
        current.record_sha256 != attempt.record_sha256
        or current_context_digest != attempt.context_sha256
        or correction_digest(attempt.record_path) != attempt.correction_sha256
    ):
        raise FakeYouError(ErrorCode.DEBRIEF_SOURCES_CHANGED)


def _build_report(
    attempt: _Attempt,
    snapshot: RecordSnapshot,
    draft: DebriefDraft,
    *,
    chunk_count: int,
    limitations: list[str],
) -> DebriefReport:
    cited = {reference.entry_id for item in draft.items for reference in item.evidence}
    cited.update(reference.entry_id for finding in draft.findings for reference in finding.evidence)
    settings = attempt.request.settings
    operator = attempt.request.operator
    return DebriefReport(
        schema_version="meeting_debrief/v1",
        owner_id=operator.owner_id,
        twin_id=operator.twin_id,
        meeting_id=attempt.request.meeting_id,
        record_sha256=snapshot.record_sha256,
        context_sha256=attempt.context_sha256,
        correction_sha256=attempt.correction_sha256,
        generated_at=datetime.now(UTC),
        provider=settings.debrief_role.provider,
        model=settings.debrief_role.model,
        source_count=len(snapshot.sources),
        chunk_count=chunk_count,
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


def _outcome(record_path: Path, *, cached: bool) -> DebriefOutcome:
    layout = MeetingDir.from_record_path(record_path)
    return DebriefOutcome(
        meeting_id=layout.meeting_id,
        cached=cached,
        json_path=layout.debrief_json,
        markdown_path=layout.debrief_md,
    )


def _is_anonymous(participant_id: str) -> bool:
    if participant_id == "unknown":
        return True
    try:
        parsed = uuid.UUID(participant_id)
    except ValueError:
        return False
    return str(parsed) == participant_id.lower()


def _correction_speakers(record_path: Path, sources: Sequence[SourceEntry]) -> list[str]:
    names = describe_correction_speakers(record_path)
    speakers: list[str] = []
    for entry in sources:
        label = names.get(entry.participant_id)
        if label is not None and label not in speakers:
            speakers.append(label)
    return speakers


def _source_limitations(
    snapshot: RecordSnapshot,
    record_path: Path,
    context: MeetingContext | None,
    *,
    fallback_used: bool,
) -> list[str]:
    limitations = [
        "Participant statements are claims, not authenticated owner decisions.",
        "Matching quotes do not prove summary entailment; independent review is required.",
        "Completed twin playback is application evidence, not a host hearing confirmation.",
        "Assignees and deadlines remain source claims; no external actions were performed.",
    ]
    if context is None:
        limitations.append("No historical meeting brief is available; objectives are unavailable.")
    if any(_is_anonymous(entry.participant_id) for entry in snapshot.sources):
        limitations.append("Unknown participants have not been identified or mapped to the owner.")
    voice_labels = voice_speaker_labels(snapshot.sources)
    if voice_labels:
        limitations.append(
            "Speaker labels are anonymous voice labels within this meeting, "
            "not verified identities: " + "; ".join(voice_labels) + "."
        )
    correction_speakers = _correction_speakers(record_path, snapshot.sources)
    if correction_speakers:
        limitations.append(
            "Correction speakers are anonymous diarization labels within this meeting, "
            "not verified identities: " + "; ".join(correction_speakers) + "."
        )
    if fallback_used:
        limitations.append(
            "Provider/model identify the configured primary; fallback may have contributed."
        )
    return limitations
