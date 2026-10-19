import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest

from fakeyou.catalog.errors import FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.ingest_job import (
    IngestionJob,
    handle_ingestion_job,
    parse_ingestion_job,
    pending_ingestion_meetings,
)
from fakeyou.memory.ingest_service import IngestPorts
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.ingestion import correction_episode_name
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import write_persona, write_record
from tests._ingestion_support import CORRECTED_AT, correction_record
from tests._memory_fakes import make_graphiti

JOB_FIELDS = {
    "v": "1",
    "type": "ingestion_job",
    "at": "2026-10-16T09:00:00Z",
    "meeting_id": "meeting-9",
    "kind": "ingest",
}


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


async def _noop_ack() -> None:
    return None


def _status_file(settings: Settings, meeting_id: str) -> Path:
    return status_path(meeting_record_path(settings.paths.record_dir, SessionId(meeting_id)))


async def test_ingest_job_acks_only_after_persisted_status(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    status_file = _status_file(settings, "meeting-9")
    acked: list[bool] = []

    async def ack() -> None:
        status = read_status(status_file)
        acked.append(status is not None and status.status == "completed")

    summary = await handle_ingestion_job(
        parse_ingestion_job(JOB_FIELDS),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(),
        ack=ack,
    )

    assert acked == [True]
    assert (summary.completed, summary.succeeded, summary.stored) == (True, True, True)


async def test_duplicate_job_adds_no_second_episode(settings: Settings, tmp_path: Path) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()
    access = _access(settings, tmp_path, memory)
    job = IngestionJob(meeting_id="meeting-9", kind="ingest")

    await handle_ingestion_job(job, access=access, ports=_ports(), ack=_noop_ack)
    await handle_ingestion_job(job, access=access, ports=_ports(), ack=_noop_ack)

    assert [episode["name"] for episode in memory.added] == ["meeting-9:w00000"]


async def test_handler_forwards_exactly_the_published_summary(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    published: list[IngestSummary] = []

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="ingest"),
        access=_access(settings, tmp_path, make_graphiti()),
        ports=_ports(published),
        ack=_noop_ack,
    )

    assert published == [summary]
    assert summary.meeting_id == "meeting-9"


async def test_incomplete_ingest_with_a_persisted_status_is_acked_but_not_succeeded(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()
    memory.errors[0] = RuntimeError("provider down")
    acks: list[str] = []

    async def ack() -> None:
        acks.append("ack")

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="ingest"),
        access=_access(settings, tmp_path, memory),
        ports=_ports(),
        ack=ack,
    )

    status = read_status(_status_file(settings, "meeting-9"))
    assert status is not None
    assert status.status == "failed"
    assert acks == ["ack"]
    assert (summary.completed, summary.succeeded, summary.stored) == (False, False, True)


async def test_correct_job_writes_only_corrected_episodes(
    settings: Settings, tmp_path: Path
) -> None:
    correction_record(settings.paths.record_dir)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    access = _access(settings, tmp_path, memory)
    await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-1", kind="ingest"),
        access=access,
        ports=_ports(),
        ack=_noop_ack,
    )
    before = [episode["name"] for episode in memory.added]
    acks: list[str] = []

    async def ack() -> None:
        acks.append("ack")

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-1", kind="correct"),
        access=access,
        ports=_ports(),
        ack=ack,
    )

    added = [episode["name"] for episode in memory.added][len(before) :]
    assert before == ["meeting-1:w00000", "meeting-1:w00001", "meeting-1:w00002"]
    assert added == [correction_episode_name("meeting-1", 1)]
    assert (summary.windows, summary.ingested) == (1, 1)
    assert (summary.completed, summary.succeeded, summary.stored) == (True, True, True)
    assert acks == ["ack"]
    status = read_status(_status_file(settings, "meeting-1"))
    assert status is not None
    assert status.corrected_at == CORRECTED_AT.isoformat()


async def test_correct_job_without_a_bundle_is_recorded_as_nothing_to_do(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    memory = make_graphiti()
    acks: list[str] = []

    async def ack() -> None:
        acks.append("ack")

    summary = await handle_ingestion_job(
        IngestionJob(meeting_id="meeting-9", kind="correct"),
        access=_access(settings, tmp_path, memory),
        ports=_ports(),
        ack=ack,
    )

    assert memory.added == []
    assert acks == ["ack"]
    assert (summary.windows, summary.ingested, summary.stored) == (0, 0, True)
    assert summary.state == "pending"


async def test_corrupt_status_is_a_failed_summary_without_ack(
    settings: Settings, tmp_path: Path
) -> None:
    write_record(settings, "meeting-9")
    status_file = _status_file(settings, "meeting-9")
    status_file.write_text("{not json", encoding="utf-8")
    memory = make_graphiti()
    acks: list[str] = []
    published: list[IngestSummary] = []

    async def ack() -> None:
        acks.append("ack")

    for job in (
        IngestionJob(meeting_id="meeting-9", kind="ingest"),
        IngestionJob(meeting_id="meeting-9", kind="correct"),
    ):
        summary = await handle_ingestion_job(
            job,
            access=_access(settings, tmp_path, memory),
            ports=_ports(published),
            ack=ack,
        )
        assert (summary.state, summary.completed, summary.succeeded, summary.stored) == (
            "failed",
            False,
            False,
            False,
        )

    assert acks == []
    assert memory.added == []
    assert status_file.read_text(encoding="utf-8") == "{not json"
    assert len(published) == 2


async def test_handler_raises_and_never_acks_when_no_summary_is_published(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fakeyou.memory.ingest_job as ingest_job_module

    async def silent(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(ingest_job_module, "run_recorded_ingest", silent)
    acks: list[str] = []

    async def ack() -> None:
        acks.append("ack")

    with pytest.raises(RuntimeError, match="ingestion job published no summary"):
        await handle_ingestion_job(
            IngestionJob(meeting_id="meeting-9", kind="ingest"),
            access=_access(settings, tmp_path, make_graphiti()),
            ports=_ports(),
            ack=ack,
        )

    assert acks == []


def test_parse_ingestion_job_accepts_the_contract_payload() -> None:
    assert parse_ingestion_job(JOB_FIELDS) == IngestionJob(meeting_id="meeting-9", kind="ingest")
    assert parse_ingestion_job(JOB_FIELDS | {"kind": "correct"}).kind == "correct"


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"kind": "reembed"}, "kind"),
        ({"v": "2"}, "v"),
        ({"type": "debrief_job"}, "type"),
        ({"meeting_id": "../meeting"}, "meeting_id"),
        ({"at": "yesterday"}, "at"),
        ({"group_id": "twin_other"}, "group_id"),
    ],
)
def test_parse_ingestion_job_rejects_fields_by_name(change: dict[str, str], field: str) -> None:
    with pytest.raises(ValueError, match=field):
        parse_ingestion_job(JOB_FIELDS | change)


def test_parse_ingestion_job_rejects_a_missing_field_by_name() -> None:
    fields = {key: value for key, value in JOB_FIELDS.items() if key != "meeting_id"}

    with pytest.raises(ValueError, match="meeting_id"):
        parse_ingestion_job(fields)


def _pending_status(meeting_id: str, status: str) -> IngestionStatus:
    return IngestionStatus.model_validate(
        {"meeting_id": meeting_id, "group_id": "twin_group", "status": status}
    )


def test_pending_ingestion_meetings_lists_only_pending_statuses(settings: Settings) -> None:
    root = settings.paths.record_dir
    for meeting_id, status in (
        ("meeting-b", "pending"),
        ("meeting-a", "pending"),
        ("meeting-c", "completed"),
    ):
        write_status(_status_file(settings, meeting_id), _pending_status(meeting_id, status))
    (root / "meeting-d").mkdir()
    (root / "meeting-e.lock").write_text("", encoding="utf-8")

    assert pending_ingestion_meetings(root) == ["meeting-a", "meeting-b"]


def test_pending_ingestion_meetings_of_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert pending_ingestion_meetings(tmp_path / "absent") == []


def test_pending_ingestion_meetings_raises_on_a_corrupt_status(settings: Settings) -> None:
    write_status(_status_file(settings, "meeting-a"), _pending_status("meeting-a", "pending"))
    corrupt = _status_file(settings, "meeting-b")
    corrupt.parent.mkdir(parents=True)
    corrupt.write_text("{", encoding="utf-8")

    with pytest.raises(FakeYouError, match="ingestion status"):
        pending_ingestion_meetings(settings.paths.record_dir)


@pytest.mark.container
async def test_redis_job_is_acked_once_and_a_redelivered_payload_adds_nothing(
    settings: Settings, tmp_path: Path, redis_endpoint: str
) -> None:
    from redis.asyncio import Redis

    write_record(settings, "meeting-9")
    memory = make_graphiti()
    access = _access(settings, tmp_path, memory)
    profile = PersonaProfile.model_validate_json(write_persona(tmp_path).read_bytes())
    stream = "fakeyou:jobs:ingestion"
    group = f"ingestion-test-{profile.twin_id}-{tmp_path.name}"
    client = Redis.from_url(redis_endpoint, decode_responses=True)
    try:
        await client.xgroup_create(stream, group, id="$", mkstream=True)
        entry_id = await client.xadd(stream, JOB_FIELDS)
        delivered = await client.xreadgroup(group, "worker-a", {stream: ">"}, count=1)
        [(_stream, [(read_id, fields)])] = delivered

        async def ack() -> None:
            await client.xack(stream, group, read_id)

        summary = await handle_ingestion_job(
            parse_ingestion_job(fields), access=access, ports=_ports(), ack=ack
        )
        pending = await client.xpending(stream, group)
        again = await handle_ingestion_job(
            parse_ingestion_job(fields), access=access, ports=_ports(), ack=_noop_ack
        )

        assert read_id == entry_id
        assert summary.stored is True
        assert pending["pending"] == 0
        assert again.stored is True
        assert [episode["name"] for episode in memory.added] == ["meeting-9:w00000"]
    finally:
        await client.xgroup_destroy(stream, group)
        await client.aclose()
