import asyncio
import contextlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import Protocol, cast
from uuid import uuid4

from pydantic import SecretStr

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.identifiers import parse_session_id
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.db.session_status import SessionStatus, SessionStatusRepository
from fakeyou.foundation.taxonomy import TERMINAL_PHASES, PublicPhase, TerminalReason
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.faults import capture_error
from fakeyou.observability.journal import Journal, JournalFile, journal_path
from fakeyou.observability.provider_faults import ProviderFault
from fakeyou.orchestration.join import run_join_session
from fakeyou.orchestration.join_types import JoinOutcome, JoinPorts, JoinRequest
from fakeyou.orchestration.runtime import SessionWiring
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.ingestion_status import read_status
from fakeyou.persistence.layout import RECORD_FILE_NAME, MeetingDir, meeting_profile_dir
from fakeyou.persistence.lease import LeaseError
from fakeyou.transport.schemas import (
    DEBRIEF_STREAM,
    INGESTION_STREAM,
    encode_debrief_job,
    encode_ingestion_job,
)
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.documents import (
    BRIEF_FILE_NAME,
    session_dir,
    write_session_document,
    write_session_persona,
)
from fakeyou.worker.keys import apply_role_overrides
from fakeyou.worker.status_writer import StatusWriter

logger = logging.getLogger(__name__)
_PAUSE_SECONDS = 1.0
SESSION_LEASE_SECONDS = 30.0
LEASE_RENEW_SECONDS = 2.0
SESSION_MAX_ATTEMPTS = 5
LEASE_SAFETY_SECONDS = 5.0
_RETRYABLE: tuple[type[Exception], ...] = (OSError, ValueError, ProviderFault, LeaseError)


class Snapshot(Protocol):
    @property
    def keys_supplied(self) -> bool: ...

    @property
    def link(self) -> str: ...

    @property
    def meeting_id(self) -> str: ...

    @property
    def owner_id(self) -> str: ...

    @property
    def twin_id(self) -> str: ...

    @property
    def twin_name(self) -> str | None: ...

    @property
    def lobby_timeout(self) -> float | None: ...

    @property
    def duration(self) -> float | None: ...

    @property
    def profile_name(self) -> str | None: ...

    @property
    def headless(self) -> bool | None: ...

    @property
    def audio(self) -> bool: ...

    @property
    def initial_speech(self) -> str | None: ...

    @property
    def conversation(self) -> bool: ...

    @property
    def persona(self) -> PersonaProfile | None: ...

    @property
    def brief(self) -> MeetingBrief | None: ...

    @property
    def memory(self) -> bool: ...

    @property
    def wake_phrase(self) -> str | None: ...

    @property
    def barge_in(self) -> bool: ...


class SnapshotStore(Protocol):
    def load(self, meeting_id: str) -> Snapshot | None: ...


@dataclass(frozen=True, slots=True)
class WorkerDeps:
    settings: Settings
    transport: StreamTransport
    store: SnapshotStore
    status: SessionStatusRepository
    ports: JoinPorts
    shutdown: asyncio.Event
    worker_id: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def join_request_for(snapshot: Snapshot, settings: Settings, *, resume: bool) -> JoinRequest:
    record_dir = settings.paths.record_dir
    persona_path = (
        write_session_persona(record_dir, snapshot.meeting_id, snapshot.persona)
        if snapshot.persona is not None
        else None
    )
    brief_path = (
        write_session_document(
            session_dir(record_dir, snapshot.meeting_id) / BRIEF_FILE_NAME, snapshot.brief
        )
        if snapshot.brief is not None
        else None
    )
    profile_dir = (
        str(
            meeting_profile_dir(
                settings.paths.browser_profile_dir,
                twin_id=parse_session_id(snapshot.twin_id, field="twin_id"),
                meeting_id=parse_session_id(snapshot.profile_name, field="profile_name"),
            )
        )
        if snapshot.profile_name is not None
        else None
    )
    return JoinRequest(
        link=snapshot.link,
        settings=settings,
        identity=OperatorIdentity(persona_path, snapshot.owner_id, snapshot.twin_id),
        audio_enabled=snapshot.audio or settings.worker.audio,
        twin_name=snapshot.twin_name,
        meeting_id=snapshot.meeting_id,
        lobby_timeout=snapshot.lobby_timeout,
        duration=snapshot.duration,
        profile_dir=profile_dir,
        headless=snapshot.headless,
        initial_speech=snapshot.initial_speech,
        memory=snapshot.memory,
        conversation_enabled=snapshot.conversation,
        wake_phrase=snapshot.wake_phrase,
        barge_in=snapshot.barge_in,
        brief_path=brief_path,
        resume=resume,
    )


@dataclass(frozen=True, slots=True)
class _Claim:
    meeting_id: str
    attempt_id: str


class WorkerConsumer:
    def __init__(self, deps: WorkerDeps) -> None:
        self._deps = deps
        self._settings = deps.settings
        self._transport = deps.transport
        self._store = deps.store
        self._status = deps.status
        self._writer: StatusWriter | None = None
        self._ports = deps.ports
        self._worker_id = deps.worker_id
        self._active: str | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._wiring_stop: asyncio.Event | None = None
        self._stop_requested = False
        self._stop_seen = False
        self._lease_lost = False
        self._bound = asyncio.Event()
        self._overrides: dict[str, dict[str, SecretStr]] = {}

    @property
    def session_task(self) -> asyncio.Task[None] | None:
        return self._session_task

    def session_settings(self, meeting_id: str) -> Settings:
        return apply_role_overrides(self._settings, self._overrides.get(meeting_id, {}))

    async def wait_session(self) -> None:
        task = self._session_task
        if task is not None:
            await asyncio.wait({task})

    async def run_forever(self) -> None:
        try:
            while not self._deps.shutdown.is_set():
                if self._session_task is None:
                    _claimed, error = await capture_error(self.run_once)
                    if error is not None:
                        logger.error("claiming a session failed", exc_info=error)
                await self._sleep_or_closed(_PAUSE_SECONDS)
        finally:
            await self._shutdown_session()

    async def run_once(self) -> str | None:
        if self._session_task is not None:
            return None
        attempt_id = uuid4().hex
        status, error = await capture_error(
            partial(self._status.claim, self._worker_id, attempt_id, SESSION_LEASE_SECONDS)
        )
        if error is not None:
            logger.warning("claiming a session failed: %s", error)
            return None
        if status is None:
            return None
        claim = _Claim(status.meeting_id, attempt_id)
        snapshot = self._store.load(claim.meeting_id)
        if status.desired_state == "stopped":
            await self._close(claim, "ended", "stopped_by_owner", "stopped before the twin joined")
            return None
        if status.attempts > SESSION_MAX_ATTEMPTS:
            detail = f"recovery gave up after {status.attempts - 1} attempts"
            await self._close(claim, "failed", "infra_failure", detail)
            return None
        if snapshot is None:
            await self._close(claim, "failed", "infra_failure", "the session snapshot is missing")
            return None
        return await self._start_join(claim, status, snapshot)

    async def _shutdown_session(self) -> None:
        task = self._session_task
        if task is None:
            return
        if self._wiring_stop is not None:
            self._wiring_stop.set()
        task.cancel()
        await asyncio.wait({task})

    async def _sleep_or_closed(self, seconds: float) -> bool:
        try:
            async with asyncio.timeout(seconds):
                await self._deps.shutdown.wait()
        except TimeoutError:
            return False
        return True

    async def _watch_lease(self, claim: _Claim) -> None:
        deadline = time.monotonic() + SESSION_LEASE_SECONDS - LEASE_SAFETY_SECONDS
        while not await self._sleep_or_closed(LEASE_RENEW_SECONDS):
            sent = time.monotonic()
            row, error = await capture_error(partial(self._renew, claim))
            if row is not None:
                deadline = sent + SESSION_LEASE_SECONDS - LEASE_SAFETY_SECONDS
                if row.desired_state == "stopped":
                    await self._request_stop(claim.meeting_id)
                continue
            if error is None or time.monotonic() >= deadline:
                logger.warning("lost the lease on meeting %s", claim.meeting_id)
                self._lease_lost = True
                self._stop_local()
                return

    async def _session_overrides(self, meeting_id: str) -> dict[str, SecretStr]:
        cached = self._overrides.get(meeting_id)
        if cached is not None:
            return cached
        taken = await self._transport.take_overrides(meeting_id)
        redeemed = {role: SecretStr(value) for role, value in taken.items()}
        self._overrides[meeting_id] = redeemed
        return redeemed

    async def _renew(self, claim: _Claim) -> SessionStatus | None:
        async with asyncio.timeout(LEASE_RENEW_SECONDS):
            return await self._status.renew(
                claim.meeting_id, self._worker_id, claim.attempt_id, SESSION_LEASE_SECONDS
            )

    async def _start_join(
        self, claim: _Claim, status: SessionStatus, snapshot: Snapshot
    ) -> str | None:
        meeting_id = claim.meeting_id
        writer = StatusWriter(self._status, meeting_id, attempt_id=claim.attempt_id)
        writer.phase = status.phase
        self._writer = writer
        await writer.write("joining")
        overrides = await self._session_overrides(meeting_id)
        session_settings = apply_role_overrides(self._settings, overrides)
        self._active = meeting_id
        self._wiring_stop = None
        self._stop_requested = False
        self._stop_seen = False
        self._lease_lost = False
        self._bound.clear()
        request = replace(
            join_request_for(snapshot, session_settings, resume=status.phase != "created"),
            on_phase=partial(self._record_live_phase, meeting_id),
        )
        task = asyncio.create_task(self._run_session(claim, request))
        self._session_task = task
        task.add_done_callback(self._session_done)
        if await self._await_binding(task):
            return meeting_id
        return None

    async def _close(
        self, claim: _Claim, phase: PublicPhase, reason: TerminalReason, detail: str
    ) -> None:
        writer = StatusWriter(self._status, claim.meeting_id, attempt_id=claim.attempt_id)
        await writer.write(phase, terminal_reason=reason, detail=detail)
        await writer.close()
        if writer.persisted != phase:
            return
        await self._publish(
            claim.meeting_id, "session_ended", {"phase": phase, "terminal_reason": reason}
        )
        await self._release(claim)

    async def _release(self, claim: _Claim) -> None:
        _released, error = await capture_error(
            partial(self._status.release, claim.meeting_id, self._worker_id, claim.attempt_id)
        )
        if error is not None:
            logger.warning("releasing meeting %s failed: %s", claim.meeting_id, error)

    async def _await_binding(self, task: asyncio.Task[None]) -> bool:
        waiter = asyncio.create_task(self._bound.wait())
        try:
            done, _pending = await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
        return waiter in done

    async def _session_port(self, config: SessionConfig, wiring: SessionWiring) -> SessionResult:
        self._wiring_stop = wiring.stop
        if self._stop_requested and wiring.stop is not None:
            wiring.stop.set()
        self._bound.set()
        return await self._ports.run_session(config, wiring)

    async def _run_session(self, claim: _Claim, request: JoinRequest) -> None:
        meeting_id = claim.meeting_id
        watcher = asyncio.create_task(self._watch_lease(claim))
        ports = JoinPorts(
            run_session=self._session_port,
            open_memory=self._ports.open_memory,
        )
        try:
            outcome, error = await capture_error(partial(run_join_session, request, ports))
            if self._lease_lost:
                logger.warning("meeting %s moved to another worker; skipping settle", meeting_id)
            elif error is None and outcome is not None:
                await self._settle(meeting_id, outcome)
            elif isinstance(error, _RETRYABLE):
                logger.warning("join for meeting %s will be retried: %s", meeting_id, error)
            elif isinstance(error, FakeYouError):
                await self._settle_failed(meeting_id, error)
            else:
                logger.error("join for meeting %s failed unexpectedly", meeting_id, exc_info=error)
                await self._settle_failed(meeting_id, cast(Exception, error))
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._forget(meeting_id)
            writer, self._writer = self._writer, None
            if writer is not None:
                await writer.close()
            if writer is not None and writer.persisted in TERMINAL_PHASES:
                await self._release(claim)

    async def _settle_failed(self, meeting_id: str, error: Exception) -> None:
        detail = str(error)
        logger.error("join for meeting %s failed: %s", meeting_id, detail)
        Journal(
            meeting_id,
            file=JournalFile(
                journal_path(
                    self._settings.paths.journal_dir,
                    parse_session_id(meeting_id, field="meeting_id"),
                )
            ),
        ).record(
            "session_failed",
            source="runtime",
            phase="failed",
            terminal_reason="infra_failure",
            error_type=type(error).__name__,
            detail=detail,
        )
        await self._apply_phase(meeting_id, "failed", "infra_failure", detail)
        await self._publish(
            meeting_id,
            "session_ended",
            {"phase": "failed", "terminal_reason": "infra_failure", "detail": detail},
        )
        self._forget(meeting_id)

    async def _settle(self, meeting_id: str, outcome: JoinOutcome) -> None:
        session = outcome.session
        await self._apply_phase(
            meeting_id, session.phase, session.terminal_reason, session.detail or None
        )
        if session.phase not in TERMINAL_PHASES:
            return
        await self._publish(
            meeting_id,
            "session_ended",
            {"phase": session.phase, "terminal_reason": session.terminal_reason},
        )
        await self._queue_pending_ingestion(meeting_id)
        if session.phase == "ended":
            await self._queue_debrief(meeting_id)
        self._forget(meeting_id)

    async def _queue_pending_ingestion(self, meeting_id: str) -> None:
        status = read_status(
            MeetingDir(
                self._settings.paths.record_dir, parse_session_id(meeting_id, field="meeting_id")
            ).ingestion
        )
        if status is None or status.status != "pending":
            return
        await self._transport.publish_job(
            INGESTION_STREAM,
            encode_ingestion_job(meeting_id=meeting_id, kind="ingest", at=_now_iso()),
        )

    async def _queue_debrief(self, meeting_id: str) -> None:
        record = self._settings.paths.record_dir
        path = session_dir(record, meeting_id) / RECORD_FILE_NAME
        if not path.exists() or not path.stat().st_size:
            return
        await self._transport.publish_job(
            DEBRIEF_STREAM,
            encode_debrief_job(meeting_id=meeting_id, at=_now_iso()),
        )

    async def _request_stop(self, meeting_id: str) -> None:
        if self._stop_seen:
            return
        self._stop_seen = True
        await self._apply_phase(meeting_id, "leaving", None)
        self._stop_local()

    def _stop_local(self) -> None:
        stop = self._wiring_stop
        if stop is None:
            self._stop_requested = True
        else:
            stop.set()

    def _record_live_phase(self, meeting_id: str, phase: PublicPhase) -> None:
        writer = self._writer
        if writer is not None and self._active == meeting_id and phase not in TERMINAL_PHASES:
            writer.note(phase)

    async def _apply_phase(
        self,
        meeting_id: str,
        phase: PublicPhase,
        terminal_reason: TerminalReason | None,
        detail: str | None = None,
    ) -> bool:
        writer = self._writer
        if writer is None:
            return False
        if not await writer.write(phase, terminal_reason=terminal_reason, detail=detail):
            return False
        await self._publish(
            meeting_id, "phase_changed", {"phase": phase, "terminal_reason": terminal_reason}
        )
        return True

    async def _publish(
        self, meeting_id: str, event_type: str, fields: Mapping[str, str | None]
    ) -> None:
        payload: dict[str, str] = {
            "v": "1",
            "type": event_type,
            "at": _now_iso(),
            "meeting_id": meeting_id,
        }
        payload.update({name: value for name, value in fields.items() if value is not None})
        await self._transport.publish_event(meeting_id, payload)

    def _forget(self, meeting_id: str) -> None:
        if self._active == meeting_id:
            self._active = None
            self._wiring_stop = None
            self._stop_requested = False

    def _session_done(self, task: asyncio.Task[None]) -> None:
        if self._session_task is task:
            self._session_task = None
        if not task.cancelled() and task.exception() is not None:
            logger.error("session task ended with an unhandled error", exc_info=task.exception())
