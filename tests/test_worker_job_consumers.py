import asyncio
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.debrief.debrief_service import DebriefOutcome
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.transport.schemas import DEBRIEF_STREAM, REEMBED_STREAM, encode_reembed_job
from fakeyou.transport.streams import PendingDelivery, StreamTransport
from fakeyou.worker.consumer import SnapshotStore
from fakeyou.worker.debrief import (
    DEBRIEF_GROUP,
    DebriefConsumer,
    DebriefDeps,
    DebriefHandler,
    DebriefJob,
    parse_debrief_job,
)
from fakeyou.worker.reembed import (
    REEMBED_GROUP,
    ReembedConsumer,
    ReembedDeps,
    ReembedHandler,
    TwinDirectory,
    TwinSnapshot,
    parse_reembed_job,
)
from tests._persistence_fakes import RecordingJournal

DEBRIEF_JOB = {
    "v": "1",
    "type": "debrief_job",
    "at": "2026-10-18T10:00:00+00:00",
    "meeting_id": "m1",
    "force": "false",
}
REEMBED_JOB = encode_reembed_job(
    at="2026-10-18T10:00:00+00:00", owner_id="owner-a", twin_id="twin-a"
)


class StreamDouble:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], dict[str, str]] = {}
        self.fresh: list[tuple[str, dict[str, str]]] = []
        self.acked: list[tuple[str, str, str]] = []
        self.events: list[tuple[str, dict[str, str]]] = []
        self.exhausted: dict[tuple[str, str], int] = {}

    def seed(self, stream: str, entry_id: str, fields: Mapping[str, str]) -> None:
        self.entries[(stream, entry_id)] = dict(fields)
        self.fresh.append((entry_id, dict(fields)))

    async def ensure_group(self, _stream: str, _group: str) -> None:
        return None

    async def pending_deliveries(
        self, _stream: str, _group: str, *, min_idle_ms: int
    ) -> list[PendingDelivery]:
        del min_idle_ms
        return []

    async def claim_entry(
        self, stream: str, _group: str, _worker: str, entry_id: str, *, min_idle_ms: int
    ) -> dict[str, str] | None:
        del min_idle_ms
        return self.entries.get((stream, entry_id))

    async def read_group(
        self, stream: str, _group: str, _worker: str, *, count: int, block_ms: int
    ) -> list[tuple[str, dict[str, str]]]:
        del count, block_ms, stream, _group, _worker
        if not self.fresh:
            return []
        return [self.fresh.pop(0)]

    async def read_entry(self, stream: str, entry_id: str) -> dict[str, str] | None:
        return self.entries.get((stream, entry_id))

    async def ack(self, stream: str, group: str, entry_id: str) -> None:
        self.acked.append((stream, group, entry_id))

    async def touch_entry(self, _stream: str, _group: str, _worker: str, _entry: str) -> None:
        return None

    async def take_overrides(self, _scope: str) -> dict[str, str]:
        return {}

    async def publish_event(self, meeting_id: str, fields: Mapping[str, str]) -> None:
        self.events.append((meeting_id, dict(fields)))

    def create_entry_ack(
        self, stream: str, group: str, entry_id: str
    ) -> Callable[[], Awaitable[None]]:
        async def ack() -> None:
            await self.ack(stream, group, entry_id)

        return ack

    @property
    def details(self) -> list[str]:
        return [fields.get("detail", "") for _meeting, fields in self.events]


class EmptyStore:
    def load(self, _meeting_id: str) -> None:
        return None

    def set_phase(
        self, _meeting_id: str, *, phase: str, terminal_reason: str | None = None
    ) -> object:
        return (phase, terminal_reason)

    def list_for(self, _owner_id: str) -> list[TwinSnapshot]:
        return []


def _outcome(meeting_id: str) -> DebriefOutcome:
    return DebriefOutcome(
        meeting_id=SessionId(meeting_id),
        cached=False,
        json_path=Path("debrief.json"),
        markdown_path=Path("debrief.md"),
    )


def _debrief_consumer(
    settings: Settings, stream: StreamDouble, handler: DebriefHandler
) -> DebriefConsumer:
    return DebriefConsumer(
        DebriefDeps(
            settings=settings,
            transport=cast(StreamTransport, stream),
            store=cast(SnapshotStore, EmptyStore()),
            shutdown=asyncio.Event(),
            worker_id="worker-a",
            handler=handler,
        )
    )


def _reembed_consumer(
    settings: Settings,
    stream: StreamDouble,
    handler: ReembedHandler,
    journal: RecordingJournal | None = None,
) -> ReembedConsumer:
    return ReembedConsumer(
        ReembedDeps(
            settings=settings,
            transport=cast(StreamTransport, stream),
            store=cast(TwinDirectory, EmptyStore()),
            shutdown=asyncio.Event(),
            worker_id="worker-a",
            handler=handler,
            journal=journal,
        )
    )


def test_reembed_job_payload_matches_the_frozen_amendment() -> None:
    assert REEMBED_JOB == {
        "v": "1",
        "type": "reembed_job",
        "at": "2026-10-18T10:00:00+00:00",
        "owner_id": "owner-a",
        "twin_id": "twin-a",
    }
    assert REEMBED_STREAM == "fakeyou:jobs:reembed"
    assert REEMBED_GROUP == "reembed-workers"
    assert DEBRIEF_GROUP == "debrief-workers"


def test_reembed_job_rejects_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown reembed job type"):
        encode_reembed_job(at="now", owner_id="o", twin_id="t", type="rebuild_job")


def test_parse_reembed_job_reads_owner_and_twin() -> None:
    job = parse_reembed_job(REEMBED_JOB)

    assert (job.owner_id, job.twin_id) == ("owner-a", "twin-a")


@pytest.mark.parametrize("missing", ["owner_id", "twin_id"])
def test_parse_reembed_job_rejects_a_payload_without_an_identity(missing: str) -> None:
    fields = {key: value for key, value in REEMBED_JOB.items() if key != missing}

    with pytest.raises(ValueError, match=f"unreadable {missing}"):
        parse_reembed_job(fields)


@pytest.mark.parametrize("force", ["true", "false"])
def test_parse_debrief_job_reads_force_from_the_frozen_string(force: str) -> None:
    assert parse_debrief_job({**DEBRIEF_JOB, "force": force}).force == (force == "true")


@pytest.mark.parametrize("meeting_id", ["", "not a id", "m-1/x"])
def test_parse_debrief_job_rejects_an_unreadable_meeting(meeting_id: str) -> None:
    with pytest.raises(ValueError, match="unreadable meeting id"):
        parse_debrief_job({**DEBRIEF_JOB, "meeting_id": meeting_id})


async def test_debrief_consumer_acks_only_after_the_handler_returns(
    settings: Settings,
) -> None:
    stream = StreamDouble()
    settled: list[str] = []

    async def handler(
        job: DebriefJob, *, ack: Callable[[], Awaitable[None]], **_kwargs: object
    ) -> DebriefOutcome:
        settled.append(job.meeting_id)
        await ack()
        return _outcome(job.meeting_id)

    stream.seed(DEBRIEF_STREAM, "0-0", DEBRIEF_JOB)

    await _debrief_consumer(settings, stream, handler).run_once()

    assert settled == ["m1"]
    assert stream.acked == [(DEBRIEF_STREAM, DEBRIEF_GROUP, "0-0")]
    assert stream.events == []


async def test_debrief_consumer_never_acks_a_failing_handler(settings: Settings) -> None:
    stream = StreamDouble()

    async def handler(
        job: DebriefJob, *, ack: Callable[[], Awaitable[None]], **_kwargs: object
    ) -> DebriefOutcome:
        del job, ack
        raise FakeYouError(ErrorCode.DEBRIEF_FAILED, reason="no such note")

    stream.seed(DEBRIEF_STREAM, "0-0", DEBRIEF_JOB)

    await _debrief_consumer(settings, stream, handler).run_once()

    assert stream.acked == []
    assert stream.details == ["debrief_job_failed"]


async def test_debrief_consumer_faults_an_unparseable_entry_without_acking(
    settings: Settings,
) -> None:
    stream = StreamDouble()
    settled: list[str] = []

    async def handler(job: DebriefJob, **_kwargs: object) -> DebriefOutcome:
        settled.append(job.meeting_id)
        return _outcome(job.meeting_id)

    stream.seed(DEBRIEF_STREAM, "0-0", {**DEBRIEF_JOB, "force": "maybe"})

    await _debrief_consumer(settings, stream, handler).run_once()

    assert settled == []
    assert stream.acked == []
    assert stream.details == ["debrief_job_invalid"]


async def test_reembed_consumer_runs_the_twin_and_acks_it(settings: Settings) -> None:
    stream = StreamDouble()
    runs: list[str] = []

    async def handler(
        access: OperatorAccess, *, ack: Callable[[], Awaitable[None]], **_kwargs: object
    ) -> str:
        runs.append(access.identity.twin_id or "")
        await ack()
        return "twin-a"

    stream.seed(REEMBED_STREAM, "0-0", REEMBED_JOB)

    await _reembed_consumer(settings, stream, handler).run_once()

    assert runs == ["twin-a"]
    assert stream.acked == [(REEMBED_STREAM, REEMBED_GROUP, "0-0")]
    assert stream.events == []


async def test_reembed_consumer_drops_a_second_job_for_the_same_twin(
    settings: Settings,
) -> None:
    stream = StreamDouble()
    runs: list[str] = []

    async def handler(
        access: OperatorAccess, *, ack: Callable[[], Awaitable[None]], **_kwargs: object
    ) -> str:
        runs.append(access.identity.twin_id or "")
        await asyncio.sleep(0)
        await ack()
        return "twin-a"

    stream.seed(REEMBED_STREAM, "0-0", REEMBED_JOB)
    stream.seed(REEMBED_STREAM, "0-1", REEMBED_JOB)
    consumer = _reembed_consumer(settings, stream, handler)

    await asyncio.gather(consumer.run_once(), consumer.run_once())

    assert runs == ["twin-a"]
    assert [entry for _stream, _group, entry in stream.acked] == ["0-0"]


async def test_reembed_consumer_faults_and_does_not_ack_a_failing_run(
    settings: Settings,
) -> None:
    stream = StreamDouble()

    async def handler(
        access: OperatorAccess, *, ack: Callable[[], Awaitable[None]], **_kwargs: object
    ) -> str:
        del access, ack
        raise FakeYouError(ErrorCode.MEMORY_FORGET_TARGET_REQUIRED)

    journal = RecordingJournal()
    stream.seed(REEMBED_STREAM, "0-0", REEMBED_JOB)

    await _reembed_consumer(settings, stream, handler, journal).run_once()

    assert stream.acked == []
    assert [row["detail"] for row in journal.events("reembed_failed")] == ["reembed_job_failed"]


async def test_debrief_fault_carries_the_domain_error_code(
    settings: Settings, journal: RecordingJournal
) -> None:
    stream = StreamDouble()
    stream.events.clear()

    async def handler(
        job: DebriefJob, *, deps: DebriefDeps, ack: Callable[[], Awaitable[None]]
    ) -> DebriefOutcome:
        del job, deps, ack
        raise FakeYouError(ErrorCode.DEBRIEF_RECORD_MISSING)

    stream.seed(DEBRIEF_STREAM, "0-0", DEBRIEF_JOB)
    deps = DebriefDeps(
        settings=settings,
        transport=cast(StreamTransport, stream),
        store=cast(SnapshotStore, EmptyStore()),
        shutdown=asyncio.Event(),
        worker_id="worker-a",
        handler=handler,
        journal=journal,
    )

    await DebriefConsumer(deps).run_once()

    [failed] = [row for _meeting, row in stream.events if row["detail"] == "debrief_job_failed"]
    assert failed["error_code"] == "debrief.record_missing"


async def test_debrief_fault_omits_the_code_for_an_unexpected_error(
    settings: Settings, journal: RecordingJournal
) -> None:
    stream = StreamDouble()
    stream.events.clear()

    async def handler(
        job: DebriefJob, *, deps: DebriefDeps, ack: Callable[[], Awaitable[None]]
    ) -> DebriefOutcome:
        del job, deps, ack
        raise ValueError("boom")

    stream.seed(DEBRIEF_STREAM, "0-0", DEBRIEF_JOB)
    deps = DebriefDeps(
        settings=settings,
        transport=cast(StreamTransport, stream),
        store=cast(SnapshotStore, EmptyStore()),
        shutdown=asyncio.Event(),
        worker_id="worker-a",
        handler=handler,
        journal=journal,
    )

    await DebriefConsumer(deps).run_once()

    [failed] = [row for _meeting, row in stream.events if row["detail"] == "debrief_job_failed"]
    assert failed["error_type"] == "ValueError"
    assert "error_code" not in failed
