import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, NoReturn

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory import ingest_service
from fakeyou.memory.ingest_service import IngestPorts, IngestRequest, run_recorded_ingest
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.ingestion import EpisodeNameError
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    hash_meeting_record,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import write_persona, write_record
from tests._memory_fakes import make_graphiti


@asynccontextmanager
async def _memory_context(memory: Any) -> AsyncIterator[Any]:
    yield memory


@contextmanager
def _no_stop_signals(_stop: asyncio.Event, _journal: Journal) -> Iterator[None]:
    yield


async def test_run_recorded_ingest_publishes_the_summary_through_its_ports(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    published: list[IngestSummary] = []
    reported: list[FakeYouError] = []
    bound: list[asyncio.Event] = []

    @contextmanager
    def bind_stop_signals(stop: asyncio.Event, _journal: Journal) -> Iterator[None]:
        bound.append(stop)
        yield

    succeeded = await run_recorded_ingest(
        IngestRequest(SessionId("meeting-9"), None, no_correct=True),
        access=OperatorAccess(
            OperatorIdentity(write_persona(tmp_path), None, None),
            settings,
            lambda: _memory_context(graphiti),
        ),
        ports=IngestPorts(
            stop_signals=bind_stop_signals,
            publish=published.append,
            report=reported.append,
        ),
    )

    assert succeeded is True
    [summary] = published
    assert (summary.state, summary.succeeded, summary.ingested) == ("completed", True, 1)
    assert len(bound) == 1
    assert reported == []
    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


async def test_run_recorded_ingest_reports_a_rejected_episode_name_and_fails(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    published: list[IngestSummary] = []
    reported: list[FakeYouError] = []

    async def rejected(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise EpisodeNameError(ErrorCode.INGESTION_EPISODE_NAME_INVALID, meeting_id="meeting-9")

    monkeypatch.setattr(ingest_service, "ingest_until_stopped", rejected)

    succeeded = await run_recorded_ingest(
        IngestRequest(SessionId("meeting-9"), None, no_correct=True),
        access=OperatorAccess(
            OperatorIdentity(write_persona(tmp_path), None, None),
            settings,
            lambda: _memory_context(make_graphiti()),
        ),
        ports=IngestPorts(
            stop_signals=_no_stop_signals,
            publish=published.append,
            report=reported.append,
        ),
    )

    assert succeeded is False
    [error] = reported
    assert error.code is ErrorCode.INGESTION_EPISODE_NAME_INVALID
    [summary] = published
    assert (summary.state, summary.succeeded) == ("failed", False)


def _ports(published: list[IngestSummary]) -> IngestPorts:
    return IngestPorts(
        stop_signals=_no_stop_signals, publish=published.append, report=lambda _error: None
    )


def _access(settings: Settings, tmp_path: Path, graphiti: Any) -> OperatorAccess:
    return OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: _memory_context(graphiti),
    )


async def test_grown_record_after_a_terminal_ingest_is_not_record_changed(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    graphiti = make_graphiti()
    access = _access(settings, tmp_path, graphiti)
    published: list[IngestSummary] = []
    request = IngestRequest(SessionId("meeting-9"), None, no_correct=True)
    await run_recorded_ingest(request, access=access, ports=_ports(published))
    first = read_status(status_path(record_path))
    assert first is not None
    assert first.record_bytes == record_path.stat().st_size
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write(
            '{"kind": "twin_speech", "at": "2026-09-30T10:05:00Z", "seq": 99, '
            '"text": "One more thing."}\n'
        )

    succeeded = await run_recorded_ingest(request, access=access, ports=_ports(published))

    status = read_status(status_path(record_path))
    assert status is not None
    assert succeeded is True
    assert (status.status, status.reason) == ("completed", None)
    assert status.record_bytes == record_path.stat().st_size
    assert published[-1].state == "completed"


async def test_a_changed_byte_inside_the_stored_prefix_is_record_changed(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    graphiti = make_graphiti()
    access = _access(settings, tmp_path, graphiti)
    published: list[IngestSummary] = []
    request = IngestRequest(SessionId("meeting-9"), None, no_correct=True)
    await run_recorded_ingest(request, access=access, ports=_ports(published))
    first = read_status(status_path(record_path))
    assert first is not None
    raw = record_path.read_bytes()
    record_path.write_bytes(raw.replace(b"Tuesday", b"Tuesdaz", 1))

    succeeded = await run_recorded_ingest(request, access=access, ports=_ports(published))

    status = read_status(status_path(record_path))
    assert status is not None
    assert succeeded is False
    assert (status.status, status.reason) == ("failed", "record_changed")
    assert (status.record_sha256, status.record_bytes) == (first.record_sha256, first.record_bytes)
    assert len(graphiti.added) == 1


async def test_whole_file_hash_without_byte_count_is_checked_once_then_rewritten(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    group = PersonaProfile.model_validate_json(write_persona(tmp_path).read_bytes()).memory_group
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="meeting-9",
            group_id=group,
            status="pending",
            record_sha256=hash_meeting_record(record_path),
        ),
    )
    published: list[IngestSummary] = []

    succeeded = await run_recorded_ingest(
        IngestRequest(SessionId("meeting-9"), None, no_correct=True),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(published),
    )

    status = read_status(status_path(record_path))
    assert status is not None
    assert succeeded is True
    assert status.record_bytes == record_path.stat().st_size
    assert status.record_sha256 == hash_meeting_record(record_path)


async def test_whole_file_hash_without_byte_count_mismatch_is_record_changed(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    group = PersonaProfile.model_validate_json(write_persona(tmp_path).read_bytes()).memory_group
    write_status(
        status_path(record_path),
        IngestionStatus(
            meeting_id="meeting-9", group_id=group, status="pending", record_sha256="0" * 64
        ),
    )
    published: list[IngestSummary] = []

    succeeded = await run_recorded_ingest(
        IngestRequest(SessionId("meeting-9"), None, no_correct=True),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(published),
    )

    status = read_status(status_path(record_path))
    assert status is not None
    assert succeeded is False
    assert (status.reason, status.record_bytes) == ("record_changed", None)
