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
from fakeyou.memory.persona import OperatorIdentity
from fakeyou.observability.journal import Journal
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
