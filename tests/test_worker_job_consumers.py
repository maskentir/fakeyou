import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import pytest
from procrastinate.testing import InMemoryConnector

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.db.jobs import JobQueue, build_job_queue
from fakeyou.debrief.debrief_service import DebriefOutcome
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.ingestion_status import IngestionStatus, write_status
from fakeyou.persistence.layout import MeetingDir
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker import debrief as debrief_worker
from fakeyou.worker.consumer import SnapshotStore
from fakeyou.worker.debrief import DebriefDeps, DebriefHandler, DebriefJob, DebriefWorker
from fakeyou.worker.reembed import (
    ReembedDeps,
    ReembedHandler,
    ReembedWorker,
    TwinDirectory,
    TwinSnapshot,
)
from tests._persistence_fakes import RecordingJournal


class JournalTailStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes) -> None:
        self.objects[key] = data

    async def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    async def list(self, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]

    async def aclose(self) -> None:
        return None


class StreamDouble:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str]]] = []

    async def take_overrides(self, _scope: str) -> dict[str, str]:
        return {}

    async def publish_event(self, meeting_id: str, fields: Mapping[str, str]) -> None:
        self.events.append((meeting_id, dict(fields)))


class EmptyStore:
    async def load(self, _meeting_id: str) -> None:
        return None

    def set_phase(
        self, _meeting_id: str, *, phase: str, terminal_reason: str | None = None
    ) -> object:
        return (phase, terminal_reason)

    async def list_for(self, _owner_id: str) -> list[TwinSnapshot]:
        return []


def _outcome(meeting_id: str) -> DebriefOutcome:
    return DebriefOutcome(
        meeting_id=SessionId(meeting_id),
        cached=False,
        json_path=Path("debrief.json"),
        markdown_path=Path("debrief.md"),
    )


def _debrief_worker(
    settings: Settings,
    stream: StreamDouble,
    handler: DebriefHandler,
    objects: JournalTailStore | None = None,
) -> tuple[DebriefWorker, JobQueue]:
    queue = build_job_queue(InMemoryConnector())
    worker = DebriefWorker(
        DebriefDeps(
            settings=settings,
            transport=cast(StreamTransport, stream),
            store=cast(SnapshotStore, EmptyStore()),
            shutdown=asyncio.Event(),
            handler=handler,
            objects=objects,
        ),
        queue,
    )
    return worker, queue


def _reembed_worker(
    settings: Settings,
    stream: StreamDouble,
    handler: ReembedHandler,
    journal: RecordingJournal | None = None,
) -> tuple[ReembedWorker, JobQueue]:
    queue = build_job_queue(InMemoryConnector())
    worker = ReembedWorker(
        ReembedDeps(
            settings=settings,
            transport=cast(StreamTransport, stream),
            store=cast(TwinDirectory, EmptyStore()),
            shutdown=asyncio.Event(),
            handler=handler,
            journal=journal,
        ),
        queue,
    )
    return worker, queue


async def test_reembed_worker_runs_the_twin_from_the_job_queue(settings: Settings) -> None:
    stream = StreamDouble()
    runs: list[str] = []

    async def handler(access: OperatorAccess, **_kwargs: object) -> str:
        runs.append(access.identity.twin_id or "")
        return "twin-a"

    worker, queue = _reembed_worker(settings, stream, handler)
    job_id = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")

    await worker.run_once()

    assert runs == ["twin-a"]
    fetched = await queue.fetch(job_id)
    assert fetched is not None
    assert fetched.status == "succeeded"
    assert stream.events == []


async def test_reembed_jobs_for_the_same_twin_never_run_at_the_same_time(
    settings: Settings,
) -> None:
    stream = StreamDouble()
    active: list[int] = []
    peak = 0

    async def handler(access: OperatorAccess, **_kwargs: object) -> str:
        nonlocal peak
        active.append(1)
        peak = max(peak, len(active))
        await asyncio.sleep(0.01)
        active.pop()
        return access.identity.twin_id or ""

    worker, queue = _reembed_worker(settings, stream, handler)
    first = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")
    second = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")

    await worker.run_once()

    assert peak == 1
    for job_id in (first, second):
        fetched = await queue.fetch(job_id)
        assert fetched is not None
        assert fetched.status == "succeeded"


async def test_reembed_worker_faults_and_keeps_a_failing_run_for_retry(
    settings: Settings,
) -> None:
    stream = StreamDouble()

    async def handler(access: OperatorAccess, **_kwargs: object) -> str:
        del access
        raise FakeYouError(ErrorCode.MEMORY_FORGET_SESSION_LIVE, meeting_id="m1")

    journal = RecordingJournal()
    worker, queue = _reembed_worker(settings, stream, handler, journal)
    job_id = await queue.defer_reembed(owner_id="owner-a", twin_id="twin-a")

    await worker.run_once()

    fetched = await queue.fetch(job_id)
    assert fetched is not None
    assert fetched.status == "todo"
    assert [row["detail"] for row in journal.events("reembed_failed")] == ["reembed_job_failed"]


async def test_debrief_worker_pushes_its_journal_tail(settings: Settings) -> None:
    stream = StreamDouble()
    text = '{"event":"debrief_completed"}\n'
    journal = journal_path(settings.paths.journal_dir, SessionId("m1"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(text, encoding="utf-8")
    store = JournalTailStore()

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del deps
        return _outcome(job.meeting_id)

    worker, queue = _debrief_worker(settings, stream, handler, objects=store)
    job_id = await queue.defer_debrief(meeting_id="m1", force=True)

    await worker.run_once()

    fetched = await queue.fetch(job_id)
    assert fetched is not None
    assert fetched.status == "succeeded"
    assert store.objects == {f"sessions/m1/journal/000000000000-{len(text):012d}": text.encode()}


async def test_debrief_worker_pushes_its_journal_tail_after_a_failure(settings: Settings) -> None:
    stream = StreamDouble()
    text = '{"event":"debrief_started"}\n'
    journal = journal_path(settings.paths.journal_dir, SessionId("m1"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(text, encoding="utf-8")
    store = JournalTailStore()

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del job, deps
        raise FakeYouError(ErrorCode.DEBRIEF_RECORD_MISSING)

    worker, queue = _debrief_worker(settings, stream, handler, objects=store)
    await queue.defer_debrief(meeting_id="m1", force=False)

    await worker.run_once()

    assert store.objects == {f"sessions/m1/journal/000000000000-{len(text):012d}": text.encode()}


async def test_debrief_worker_waits_for_the_ingestion_to_settle(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(debrief_worker, "DEBRIEF_INGESTION_POLL_SECONDS", 0.01)
    monkeypatch.setattr(debrief_worker, "DEBRIEF_INGESTION_WAIT_SECONDS", 5.0)
    stream = StreamDouble()
    seen: list[DebriefJob] = []

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del deps
        seen.append(job)
        return _outcome(job.meeting_id)

    worker, queue = _debrief_worker(settings, stream, handler)
    status = MeetingDir(settings.paths.record_dir, SessionId("m1")).ingestion
    write_status(status, IngestionStatus(meeting_id="m1", group_id="g", status="pending"))
    job_id = await queue.defer_debrief(meeting_id="m1", force=True)

    running = asyncio.create_task(worker.run_once())
    await asyncio.sleep(0.05)
    assert seen == []

    write_status(status, IngestionStatus(meeting_id="m1", group_id="g", status="completed"))
    await running

    assert seen == [DebriefJob(meeting_id="m1", force=True)]
    fetched = await queue.fetch(job_id)
    assert fetched is not None
    assert fetched.status == "succeeded"


async def test_debrief_worker_runs_the_meeting_from_the_job_queue(
    settings: Settings,
) -> None:
    stream = StreamDouble()
    seen: list[DebriefJob] = []

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del deps
        seen.append(job)
        return _outcome(job.meeting_id)

    worker, queue = _debrief_worker(settings, stream, handler)
    job_id = await queue.defer_debrief(meeting_id="m1", force=True)

    await worker.run_once()

    assert seen == [DebriefJob(meeting_id="m1", force=True)]
    fetched = await queue.fetch(job_id)
    assert fetched is not None
    assert fetched.status == "succeeded"


async def test_debrief_fault_carries_the_domain_error_code(settings: Settings) -> None:
    stream = StreamDouble()
    stream.events.clear()

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del job, deps
        raise FakeYouError(ErrorCode.DEBRIEF_RECORD_MISSING)

    worker, queue = _debrief_worker(settings, stream, handler)
    await queue.defer_debrief(meeting_id="m1", force=False)

    await worker.run_once()

    [failed] = [row for _meeting, row in stream.events if row["detail"] == "debrief_job_failed"]
    assert failed["error_code"] == "debrief.record_missing"


async def test_debrief_fault_omits_the_code_for_an_unexpected_error(settings: Settings) -> None:
    stream = StreamDouble()
    stream.events.clear()

    async def handler(job: DebriefJob, *, deps: DebriefDeps) -> DebriefOutcome:
        del job, deps
        raise ValueError("boom")

    worker, queue = _debrief_worker(settings, stream, handler)
    await queue.defer_debrief(meeting_id="m1", force=False)

    await worker.run_once()

    [failed] = [row for _meeting, row in stream.events if row["detail"] == "debrief_job_failed"]
    assert failed["error_type"] == "ValueError"
    assert "error_code" not in failed
