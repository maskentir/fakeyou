import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.ingest_job import (
    IngestionJob,
    handle_ingestion_job,
)
from fakeyou.memory.ingest_service import IngestPorts
from fakeyou.memory.ingest_status import IngestSummary, settle_correction
from fakeyou.memory.ingestion import correction_episode_name
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    SettledJob,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import write_persona, write_record
from tests._ingestion_support import CORRECTED_AT, correction_record
from tests._memory_fakes import make_graphiti


@asynccontextmanager
async def _memory_context(memory: Any) -> AsyncIterator[Any]:
    yield memory


def _ports(published: list[IngestSummary] | None = None) -> IngestPorts:
    @contextmanager
    def no_stop_signals(_stop: asyncio.Event, _journal: Journal) -> Iterator[None]:
        yield

    sink = published if published is not None else []
    return IngestPorts(stop_signals=no_stop_signals, publish=sink.append, report=lambda _e: None)


def _access(settings: Settings, tmp_path: Path, memory: Any) -> OperatorAccess:
    return OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: _memory_context(memory),
    )


def _status_file(settings: Settings, meeting_id: str) -> Path:
    return status_path(meeting_record_path(settings.paths.record_dir, SessionId(meeting_id)))


async def test_ingest_job_returns_only_after_persisted_status(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    status_file = _status_file(settings, "meeting-9")

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="7"),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(),
    )

    status = read_status(status_file)
    assert status is not None
    assert status.status == "completed"
    assert status.settled_job == SettledJob(id="7", outcome="completed")
    assert (summary.completed, summary.succeeded, summary.stored) == (True, True, True)


async def test_duplicate_job_adds_no_second_episode(settings: Settings, tmp_path: Path) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()
    access = _access(settings, tmp_path, memory)
    job = IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="1-0")

    await handle_ingestion_job(job, access=access, ports=_ports())
    await handle_ingestion_job(job, access=access, ports=_ports())

    assert [episode["name"] for episode in memory.added] == ["meeting-9:w00000"]


async def test_handler_forwards_exactly_the_published_summary(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    published: list[IngestSummary] = []

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="1-0"),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(published),
    )

    assert published == [summary]
    assert summary.meeting_id == "meeting-9"


async def test_incomplete_ingest_with_a_persisted_status_is_settled_but_not_succeeded(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()
    memory.errors[0] = RuntimeError("provider down")
    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="1-0"),
        access=_access(settings, tmp_path, memory),
        ports=_ports(),
    )

    status = read_status(_status_file(settings, "meeting-9"))
    assert status is not None
    assert status.status == "failed"
    assert (summary.completed, summary.succeeded, summary.stored) == (False, False, True)


async def test_correct_job_writes_only_corrected_episodes(
    settings: Settings, tmp_path: Path
) -> None:
    correction_record(settings.paths.record_dir)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    access = _access(settings, tmp_path, memory)
    await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-1", kind="ingest", job_id="7"),
        access=access,
        ports=_ports(),
    )
    ingested = read_status(_status_file(settings, "meeting-1"))
    assert ingested is not None
    assert (ingested.status, ingested.settled_job) == (
        "completed",
        SettledJob(id="7", outcome="completed"),
    )
    before = [episode["name"] for episode in memory.added]
    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-1", kind="correct", job_id="8"),
        access=access,
        ports=_ports(),
    )

    added = [episode["name"] for episode in memory.added][len(before) :]
    assert before == ["meeting-1:w00000", "meeting-1:w00001", "meeting-1:w00002"]
    assert added == [correction_episode_name("meeting-1", 1)]
    assert (summary.windows, summary.ingested) == (1, 1)
    assert (summary.completed, summary.succeeded, summary.stored) == (True, True, True)
    status = read_status(_status_file(settings, "meeting-1"))
    assert status is not None
    assert status.corrected_at == CORRECTED_AT.isoformat()
    assert (status.status, status.settled_job, status.episode_uuids) == (
        "completed",
        SettledJob(id="8", outcome="completed"),
        ingested.episode_uuids,
    )


def _correction_summary(*, succeeded: bool, stored: bool) -> IngestSummary:
    return IngestSummary(
        meeting_id="meeting-1",
        state="completed",
        windows=1,
        resumed_from=0,
        ingested=0,
        facts=0,
        completed=succeeded,
        succeeded=succeeded,
        stored=stored,
    )


def _ingested_status(status_file: Path) -> IngestionStatus:
    status = IngestionStatus(
        meeting_id="meeting-1",
        group_id="g",
        status="completed",
        episode_uuids={0: "uuid-a"},
        settled_job=SettledJob(id="7", outcome="completed"),
    )
    write_status(status_file, status)
    return status


def test_settled_failed_correction_fails_its_job_and_keeps_the_meeting_state(
    tmp_path: Path,
) -> None:
    status_file = tmp_path / "ingestion.json"
    _ingested_status(status_file)

    summary = settle_correction(
        status_file,
        _correction_summary(succeeded=False, stored=True),
        job_id="8",
        group_id="g",
        faults=FaultReporter(journal=Journal("meeting-1")),
    )

    status = read_status(status_file)
    assert status is not None
    assert status.settled_job == SettledJob(id="8", outcome="failed")
    assert (status.status, status.episode_uuids) == ("completed", {0: "uuid-a"})
    assert (summary.state, summary.succeeded, summary.stored) == ("completed", False, True)


def test_unstored_correction_leaves_its_job_pending_for_the_retry(tmp_path: Path) -> None:
    status_file = tmp_path / "ingestion.json"
    before = _ingested_status(status_file)

    settle_correction(
        status_file,
        _correction_summary(succeeded=False, stored=False),
        job_id="8",
        group_id="g",
        faults=FaultReporter(journal=Journal("meeting-1")),
    )

    assert read_status(status_file) == before


async def test_correct_job_without_a_bundle_is_recorded_as_nothing_to_do(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="correct", job_id="7"),
        access=_access(settings, tmp_path, memory),
        ports=_ports(),
    )

    assert memory.added == []
    assert (summary.windows, summary.ingested, summary.stored) == (0, 0, True)
    assert summary.state == "not_requested"
    status = read_status(_status_file(settings, "meeting-9"))
    assert status is not None
    assert status.status == "not_requested"
    assert status.settled_job == SettledJob(id="7", outcome="completed")


async def test_corrupt_status_raises_so_the_job_is_retried(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    status_file = _status_file(settings, "meeting-9")
    status_file.write_text("{not json", encoding="utf-8")
    memory = make_graphiti()
    published: list[IngestSummary] = []

    for job in (
        IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="7"),
        IngestionJob(meeting_id="meeting-9", kind="correct", job_id="7"),
    ):
        with pytest.raises(RuntimeError, match="unstored summary"):
            await handle_ingestion_job(
                job,
                access=_access(settings, tmp_path, memory),
                ports=_ports(published),
            )

    assert memory.added == []
    assert status_file.read_text(encoding="utf-8") == "{not json"
    assert [(item.state, item.stored) for item in published] == [("failed", False)] * 2


async def test_handler_raises_when_no_summary_is_published(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.memory.ingest_job as ingest_job_module

    async def silent(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(ingest_job_module, "run_recorded_ingest", silent)

    with pytest.raises(RuntimeError, match="ingestion job published no summary"):
        await handle_ingestion_job(
            IngestionJob(meeting_id="meeting-9", kind="ingest", job_id="7"),
            access=_access(settings, tmp_path, make_graphiti()),
            ports=_ports(),
        )
