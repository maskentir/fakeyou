import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from procrastinate import RetryStrategy
from procrastinate.testing import InMemoryConnector
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from fakeyou.api.schemas import BriefDocument, SessionCreate, SessionSnapshot, build_snapshot
from fakeyou.api.sessions import SessionStore
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.db.jobs import INGEST_QUEUE, INGEST_TASK, QueuedJob, build_job_queue
from fakeyou.db.session_status import DesiredState, PhaseUpdate, SessionStatus
from fakeyou.foundation.taxonomy import PublicPhase, TerminalReason
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory.ingest_job import IngestionJob, handle_ingestion_job
from fakeyou.memory.ingest_service import IngestPorts, IngestRequest
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.join_types import JoinOutcome, JoinPorts, JoinRequest
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    SettledJob,
    read_status,
    record_lock,
    write_status,
)
from fakeyou.persistence.layout import MeetingDir, meeting_profile_dir
from fakeyou.persistence.owner_model import FrozenOwnerModel
from fakeyou.persistence.record import MeetingRecord
from fakeyou.persistence.segments import ObjectStore
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.__main__ import run_consumers, verify_boot_dependencies
from fakeyou.worker.consumer import (
    SESSION_MAX_ATTEMPTS,
    WorkerConsumer,
    WorkerDeps,
    join_request_for,
)
from fakeyou.worker.ingestion import (
    DEAD_LETTER_DETAIL,
    FAILED_JOB_DETAIL,
    INGESTION_DELIVERY_LIMIT,
    INVALID_JOB_DETAIL,
    IngestionDeps,
    IngestionWorker,
)
from tests._session_status_fakes import MemorySessionStatus

AT = "2026-10-16T09:00:00+00:00"
STATUS_AT = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
LINK = "https://meet.google.com/abc-defg-hij"
MARKER_REPLY_KEY = "marker-reply-731"
PERSONA_DATA = {
    "owner_id": "owner-1",
    "twin_id": "twin-1",
    "display_name": "Demo Twin",
    "role": "Synthetic project lead",
    "allowed_tools": ["recall_meeting_context"],
}
BRIEF_DATA = {
    "schema_version": "meeting_brief/v1",
    "owner_id": "owner-1",
    "twin_id": "twin-1",
    "agenda": ["Synthetic kickoff"],
    "objectives": [{"id": "blockers", "text": "Find the synthetic blocker."}],
    "permissions": ["explain_facts", "ask_brief_questions"],
    "knowledge_scope": "brief_only",
}


def _stream_key(entry_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = entry_id.partition("-")
    return int(milliseconds), int(sequence or "0")


def _in_range(entry_id: str, minimum: str, maximum: str) -> bool:
    key = _stream_key(entry_id)
    if minimum.startswith("("):
        above = key > _stream_key(minimum[1:])
    else:
        above = minimum == "-" or key >= _stream_key(minimum)
    below = maximum == "+" or key <= _stream_key(maximum)
    return above and below


@dataclass
class PendingEntry:
    entry_id: str
    fields: dict[str, str]
    consumer: str
    idle: int = 0
    deliveries: int = 1


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.groups: dict[tuple[str, str], int] = {}
        self.pending: dict[tuple[str, str], list[PendingEntry]] = {}
        self.leases: dict[str, str] = {}
        self.acks: list[tuple[str, str]] = []
        self.events: list[dict[str, str]] = []
        self.log: list[str] = []
        self.failing_reads = 0

    def age(self, milliseconds: int) -> None:
        for entries in self.pending.values():
            for entry in entries:
                entry.idle += milliseconds

    def pending_for(self, name: str, groupname: str) -> list[PendingEntry]:
        return self.pending.get((name, groupname), [])

    async def xadd(
        self,
        name: str,
        fields: Mapping[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self.calls.append(("xadd", name, maxlen, approximate))
        entry_id = f"{len(self.streams.get(name, [])) + 1}-0"
        self.streams.setdefault(name, []).append((entry_id, dict(fields)))
        if name.endswith(":events"):
            self.events.append(dict(fields))
        return entry_id

    async def getdel(self, name: str) -> str | None:
        self.calls.append(("getdel", name))
        return self.leases.pop(name, None)

    async def set(
        self, name: str, value: str, *, nx: bool = False, px: int | None = None
    ) -> bool | None:
        self.calls.append(("set", name, value, px))
        if nx and name in self.leases:
            return None
        self.leases[name] = value
        return True

    async def eval(self, script: str, _numkeys: int, *keys_and_args: str) -> int:
        key, owner = keys_and_args[0], keys_and_args[1]
        if self.leases.get(key) != owner:
            return 0
        if "PEXPIRE" in script:
            return 1
        del self.leases[key]
        return 1

    async def xgroup_create(
        self, name: str, groupname: str, *, id: str = "$", mkstream: bool = False
    ) -> bool:
        self.calls.append(("xgroup_create", name, groupname, id, mkstream))
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups[(name, groupname)] = 0
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: Mapping[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[list[object]]:
        self.calls.append(("xreadgroup", groupname, consumername, count, block))
        if self.failing_reads:
            self.failing_reads -= 1
            raise RedisConnectionError("connection reset by peer")
        stream = next(iter(streams))
        entries = self.streams.get(stream, [])
        start = self.groups.get((stream, groupname), 0)
        fresh = entries[start : start + (count or 1)]
        self.groups[(stream, groupname)] = start + len(fresh)
        for entry_id, fields in fresh:
            self.pending.setdefault((stream, groupname), []).append(
                PendingEntry(entry_id, fields, consumername)
            )
        if not fresh:
            return []
        return [[stream, fresh]]

    async def xack(self, name: str, groupname: str, *entry_ids: str) -> int:
        entries = self.pending.get((name, groupname), [])
        removed = 0
        for entry_id in entry_ids:
            self.log.append(f"ack:{entry_id}")
            for index, entry in enumerate(entries):
                if entry.entry_id == entry_id:
                    entries.pop(index)
                    self.acks.append(
                        (entry.fields.get("type", ""), entry.fields.get("meeting_id", ""))
                    )
                    removed += 1
                    break
        return removed

    async def xpending_range(
        self, name: str, groupname: str, *range_args: str | int, idle: int | None = None
    ) -> list[dict[str, object]]:
        _minimum, _maximum, count = range_args
        entries = self.pending.get((name, groupname), [])
        relaxed = str(range_args[0]) == "-" and str(range_args[1]) == "+"
        self.calls.append(("xpending_range", name, groupname, relaxed, count, idle))
        selected = [entry for entry in entries if entry.idle >= (idle or 0)][: int(count)]
        return [
            {
                "message_id": entry.entry_id,
                "consumer": entry.consumer,
                "time_since_delivered": entry.idle,
                "times_delivered": entry.deliveries,
            }
            for entry in selected
        ]

    async def xautoclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        *,
        count: int,
    ) -> list[object]:
        idle = [
            entry.entry_id
            for entry in self.pending.get((name, groupname), [])
            if entry.idle >= min_idle_time
        ][:count]
        claimed = await self.xclaim(name, groupname, consumername, min_idle_time, idle)
        return ["0-0", claimed, []]

    async def xclaim(
        self,
        name: str,
        groupname: str,
        consumername: str,
        min_idle_time: int,
        message_ids: list[str],
        **options: bool,
    ) -> list[tuple[str, dict[str, str]]] | list[str]:
        justid = options.get("justid", False)
        self.calls.append(("xclaim", name, groupname, consumername, justid))
        claimed: list[PendingEntry] = []
        for entry in self.pending.get((name, groupname), []):
            if entry.entry_id in message_ids and entry.idle >= min_idle_time:
                entry.consumer = consumername
                entry.idle = 0
                entry.deliveries += 0 if justid else 1
                claimed.append(entry)
        if justid:
            return [entry.entry_id for entry in claimed]
        return [(entry.entry_id, entry.fields) for entry in claimed]

    async def xinfo_groups(self, name: str) -> list[dict[str, object]]:
        entries = self.streams.get(name, [])
        return [
            {
                "name": groupname,
                "last-delivered-id": entries[delivered - 1][0] if delivered else "0-0",
            }
            for (stream, groupname), delivered in self.groups.items()
            if stream == name
        ]

    async def xrange(
        self, name: str, min: str = "-", max: str = "+", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = [
            (entry_id, fields)
            for entry_id, fields in self.streams.get(name, [])
            if _in_range(entry_id, min, max)
        ]
        return entries[:count] if count is not None else entries

    async def scan(
        self, cursor: int = 0, *, match: str | None = None, count: int | None = None
    ) -> tuple[int, list[str]]:
        self.calls.append(("scan", cursor, match, count))
        names = [name for name in self.streams if match is None or fnmatchcase(name, match)]
        return 0, sorted(names)

    async def aclose(self) -> None:
        self.calls.append(("aclose",))


class SessionDouble:
    def __init__(self) -> None:
        self.runs: list[tuple[str, bool]] = []
        self.attempts: list[str | None] = []
        self.stop_event: asyncio.Event = asyncio.Event()
        self.outcome = SessionResult(
            phase="ended", terminal_reason="stopped_by_owner", detail="harness"
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.crash = False
        self.live_phases: list[PublicPhase] = []

    async def run(self, config: SessionConfig, wiring: SessionWiring) -> SessionResult:
        if wiring.stop is not None:
            self.stop_event = wiring.stop
        self.runs.append((str(config.meeting_id), bool(config.resume)))
        self.attempts.append(wiring.kernel.attempt_id if wiring.kernel is not None else None)
        self.started.set()
        await self.release.wait()
        self.started.clear()
        self.release.clear()
        if self.crash:
            self.crash = False
            raise OSError("session crashed")
        if wiring.on_phase is not None:
            for phase in self.live_phases:
                wiring.on_phase(phase)
        return self.outcome


def _transcript(event_id: str, text: str) -> TranscriptEvent:
    return TranscriptEvent(
        id=event_id,
        timestamp="2026-10-28T10:00:00+00:00",
        source="meeting_audio",
        text=text,
        finalized=True,
        participant_id="device-ayu",
        speaker_evidence_id=None,
    )


def unused_memory(_settings: Settings) -> Any:
    pytest.fail("memory must stay closed")


def unused_checkpointer(*_args: object, **_kwargs: object) -> Any:
    pytest.fail("checkpointer must stay closed")


class SnapshotReader(Protocol):
    async def load(self, meeting_id: str) -> SessionSnapshot | None: ...


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = FakeClient()
        self.transport = StreamTransport(cast(Redis, self.client), stream_max_len=1000)
        self.status = MemorySessionStatus()
        self.session_store = SessionStore(self.status)
        self.store: SnapshotReader = cast(SnapshotReader, self.session_store)
        self.jobs = build_job_queue(InMemoryConnector())
        self.double = SessionDouble()
        self.objects: ObjectStore | None = None
        self.ports = JoinPorts(
            run_session=self.double.run,
            open_memory=unused_memory,
        )
        self.consumer = self.member(settings, "worker-a")
        self.other_consumer = self.member(settings, "worker-b")

    def member(self, settings: Settings, worker_id: str) -> WorkerConsumer:
        return WorkerConsumer(
            WorkerDeps(
                settings=settings,
                transport=self.transport,
                store=self.session_store,
                status=self.status,
                ports=self.ports,
                jobs=self.jobs,
                shutdown=asyncio.Event(),
                worker_id=worker_id,
                objects=self.objects,
            )
        )

    async def stage_overrides(self, meeting_id: str, values: dict[str, str]) -> None:
        await self.transport.store_overrides(meeting_id, values, ttl_seconds=120.0)

    @property
    def runs(self) -> list[tuple[str, bool]]:
        return self.double.runs

    @property
    def acks(self) -> list[tuple[str, str]]:
        return self.client.acks

    @property
    def events(self) -> list[dict[str, str]]:
        return self.client.events

    @property
    def stop_event(self) -> asyncio.Event:
        return self.double.stop_event

    async def seed(self, meeting_id: str) -> None:
        snapshot = build_snapshot(
            SessionCreate(link=LINK, owner_id="owner-1", twin_id="twin-1"),
            meeting_id=meeting_id,
            at=AT,
        )
        await self.status.create(
            meeting_id, "owner-1", STATUS_AT, payload=snapshot.model_dump(mode="json")
        )

    async def phase(self, meeting_id: str) -> str | None:
        status = await self.status.get(meeting_id)
        return None if status is None else status.phase

    async def enqueue(self, meeting_id: str, kind: str) -> None:
        wished: dict[str, DesiredState] = {"join": "joined", "stop": "stopped"}
        if kind in wished:
            await self.status.desire(meeting_id, wished[kind], STATUS_AT)

    def persist(self, meeting_id: str, state: str) -> None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        write_status(
            path,
            IngestionStatus(meeting_id=meeting_id, group_id="g", status=cast(Any, state)),
        )

    async def finish_session(self, *, phase: PublicPhase, terminal_reason: TerminalReason) -> None:
        await self.double.started.wait()
        self.double.outcome = SessionResult(
            phase=phase, terminal_reason=terminal_reason, detail="harness"
        )
        self.double.release.set()
        await self.consumer.wait_session()
        self.double.started.clear()
        self.double.release.clear()

    async def crash_session(self) -> None:
        await self.double.started.wait()
        self.double.crash = True
        self.double.release.set()
        await self.consumer.wait_session()
        self.double.started.clear()
        self.double.release.clear()

    async def aclose(self) -> None:
        tasks = [
            task
            for task in (self.consumer.session_task, self.other_consumer.session_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)


@pytest.fixture(autouse=True)
def quick_lease_renewal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fakeyou.worker.consumer.LEASE_RENEW_SECONDS", 0.01)


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[Harness]:
    harness = Harness(settings)
    await harness.seed("m-1")
    try:
        yield harness
    finally:
        await harness.aclose()


async def test_one_join_runs_one_session_and_acks_after_phase_persisted(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert harness.runs == [("m-1", False)]
    assert await harness.phase("m-1") == "ended"
    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]


async def test_a_join_that_fails_before_the_session_starts_settles_as_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_before_binding(*_args: object) -> JoinOutcome:
        raise FakeYouError(ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH)

    monkeypatch.setattr("fakeyou.worker.consumer.run_join_session", fail_before_binding)
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() is None
    assert await harness.phase("m-1") == "failed"
    assert harness.events[-1] == {
        "v": "1",
        "type": "session_ended",
        "at": harness.events[-1]["at"],
        "meeting_id": "m-1",
        "phase": "failed",
        "terminal_reason": "infra_failure",
        "detail": harness.events[-1]["detail"],
    }
    assert "persona" in harness.events[-1]["detail"]
    assert harness.status.leases == {}


async def test_second_worker_cannot_claim_the_same_meeting(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    assert await harness.other_consumer.run_once() is None
    assert harness.runs == [("m-1", False)]


async def test_an_owner_stop_reaches_the_running_session_through_the_lease(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    await harness.consumer.run_once()
    await harness.double.started.wait()
    await harness.enqueue("m-1", "stop")

    await asyncio.wait_for(harness.stop_event.wait(), timeout=1)

    assert await harness.phase("m-1") == "leaving"


async def test_a_crashed_session_is_claimed_again_and_resumed(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.crash_session()
    harness.status.expired.add("m-1")
    assert await harness.consumer.run_once() == "m-1"
    assert harness.runs == [("m-1", False), ("m-1", True)]
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert await harness.phase("m-1") == "ended"
    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]


class FakeObjectStore:
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


async def test_a_takeover_restores_the_record_from_segments_and_flushes_new_ones(
    harness: Harness,
) -> None:
    objects = FakeObjectStore()
    harness.objects = objects
    consumer = harness.member(harness.settings, "worker-c")
    record = MeetingDir(harness.settings.paths.record_dir, SessionId("m-1")).record
    seeded = MeetingRecord(record, now=lambda: STATUS_AT)
    seeded.add_transcript(_transcript("event-1", "Seed line."))
    seeded.close()
    seed_bytes = record.read_bytes()
    record.unlink()
    objects.objects[f"sessions/m-1/record/000000000000-{len(seed_bytes):012d}"] = seed_bytes
    await harness.status.record_phase("m-1", PhaseUpdate("joining", STATUS_AT))
    await harness.enqueue("m-1", "join")

    assert await consumer.run_once() == "m-1"
    await harness.double.started.wait()

    assert record.read_bytes() == seed_bytes

    resumed = MeetingRecord(record, now=lambda: STATUS_AT)
    resumed.add_transcript(_transcript("event-2", "After takeover."))
    resumed.close()
    harness.double.release.set()
    await consumer.wait_session()

    tail = record.read_bytes()[len(seed_bytes) :]
    key = f"sessions/m-1/record/{len(seed_bytes):012d}-{len(seed_bytes) + len(tail):012d}"
    assert objects.objects[key] == tail


async def test_each_claim_runs_its_session_under_the_claimed_attempt(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.double.started.wait()
    first = harness.status.rows["m-1"].attempt_id
    await harness.crash_session()
    harness.status.expired.add("m-1")
    assert await harness.other_consumer.run_once() == "m-1"
    await harness.double.started.wait()
    second = harness.status.rows["m-1"].attempt_id

    assert first is not None
    assert second is not None
    assert first != second
    assert harness.double.attempts == [first, second]
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")


async def test_join_request_for_writes_persona_and_brief_server_side(settings: Settings) -> None:
    persona = PersonaProfile.model_validate(PERSONA_DATA)
    create = SessionCreate(
        link=LINK,
        meeting_id="m-9",
        owner_id="owner-1",
        twin_id="twin-1",
        twin_name="Demo Twin",
        lobby_timeout=12.0,
        profile_name="prof-1",
        audio=True,
        conversation=True,
        initial_speech="Hello everyone",
        wake_phrase="hey twin",
        barge_in=True,
        persona=persona,
        brief=BriefDocument.model_validate(BRIEF_DATA),
    )
    snapshot = build_snapshot(create, meeting_id="m-9", at=AT)

    request = join_request_for(snapshot, settings, resume=True)

    assert request.link == LINK
    assert request.meeting_id == "m-9"
    assert request.resume is True
    assert request.audio_enabled is True
    assert request.conversation_enabled is True
    assert request.twin_name == "Demo Twin"
    assert request.lobby_timeout == 12.0
    assert request.initial_speech == "Hello everyone"
    assert request.wake_phrase == "hey twin"
    assert request.barge_in is True
    assert request.participant_policy_enabled is True
    assert request.participant_evidence_enabled is False
    assert request.headless is None
    assert request.profile_dir == str(
        meeting_profile_dir(
            settings.paths.browser_profile_dir,
            twin_id=SessionId("twin-1"),
            meeting_id=SessionId("prof-1"),
        )
    )
    meeting_dir = MeetingDir(settings.paths.record_dir, SessionId("m-9"))
    assert request.identity.persona_path == meeting_dir.dir / "worker-persona.json"
    assert request.brief_path == meeting_dir.dir / "worker-brief.json"
    persona_path = request.identity.persona_path
    assert persona_path is not None
    assert PersonaProfile.model_validate_json(persona_path.read_bytes()) == persona
    assert request.brief_path is not None
    assert MeetingBrief.model_validate_json(request.brief_path.read_bytes()) == snapshot.brief


def test_a_stored_snapshot_whose_persona_holds_owner_knowledge_is_refused() -> None:
    create = SessionCreate(
        link=LINK,
        owner_id="owner-1",
        twin_id="twin-1",
        audio=True,
        conversation=True,
        persona=PersonaProfile.model_validate(PERSONA_DATA),
    )
    stored = build_snapshot(create, meeting_id="m-4", at=AT).model_dump(mode="json")
    stored["persona"]["goals"] = ["Keep the weekly sync short"]

    with pytest.raises(FakeYouError, match="persona fields goals") as raised:
        SessionSnapshot.model_validate(stored)

    assert raised.value.code is ErrorCode.PERSONA_KNOWLEDGE_MOVED


async def test_join_request_for_carries_the_frozen_owner_model(settings: Settings) -> None:
    owner_model = FrozenOwnerModel(version=2)
    create = SessionCreate(link=LINK, owner_id="owner-1", twin_id="twin-1")

    frozen = build_snapshot(create, meeting_id="m-3", at=AT, owner_model=owner_model)
    plain = build_snapshot(create, meeting_id="m-3", at=AT)

    assert join_request_for(frozen, settings, resume=False).owner_model == owner_model
    assert join_request_for(plain, settings, resume=False).owner_model is None


async def test_join_request_for_applies_the_worker_audio_default(settings: Settings) -> None:
    snapshot = build_snapshot(
        SessionCreate(link=LINK, owner_id="owner-1", twin_id="twin-1"),
        meeting_id="m-2",
        at=AT,
    )
    settings.worker.audio = True

    request = join_request_for(snapshot, settings, resume=False)

    assert request.audio_enabled is True
    assert request.resume is False
    assert request.profile_dir is None
    assert request.identity.persona_path is None
    assert request.brief_path is None


def _record_settings(monkeypatch: pytest.MonkeyPatch, seen: list[Settings]) -> None:
    original = join_request_for

    def recording(snapshot: SessionSnapshot, settings: Settings, *, resume: bool) -> JoinRequest:
        seen.append(settings)
        return original(snapshot, settings, resume=resume)

    monkeypatch.setattr("fakeyou.worker.consumer.join_request_for", recording)


async def test_join_takes_session_keys_once_and_builds_a_settings_copy(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.stage_overrides("m-1", {"reply": MARKER_REPLY_KEY})
    seen: list[Settings] = []
    _record_settings(monkeypatch, seen)
    await harness.enqueue("m-1", "join")

    assert await harness.consumer.run_once() == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    assert await harness.transport.take_overrides("m-1") == {}
    assert [event["type"] for event in harness.events] == [
        "phase_changed",
        "session_ended",
    ]
    copy_reply = seen[0].reply.api_key
    process_reply = harness.settings.reply.api_key
    assert copy_reply is not None
    assert process_reply is not None
    assert copy_reply.get_secret_value() == MARKER_REPLY_KEY
    assert process_reply.get_secret_value() == "test-key"


async def test_join_without_staged_keys_keeps_the_process_keys(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Settings] = []
    _record_settings(monkeypatch, seen)
    await harness.enqueue("m-1", "join")

    assert await harness.consumer.run_once() == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]
    session_reply = seen[0].reply.api_key
    assert session_reply is not None
    assert session_reply.get_secret_value() == "test-key"


async def test_session_keys_are_taken_once_across_resume(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.stage_overrides("m-1", {"reply": MARKER_REPLY_KEY})
    seen: list[Settings] = []
    _record_settings(monkeypatch, seen)
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.crash_session()
    harness.status.expired.add("m-1")

    assert await harness.consumer.run_once() == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    assert await harness.transport.take_overrides("m-1") == {}
    resumed_reply = seen[1].reply.api_key
    assert resumed_reply is not None
    assert resumed_reply.get_secret_value() == MARKER_REPLY_KEY


def _summary(meeting_id: str, *, state: str, completed: bool, stored: bool) -> IngestSummary:
    return IngestSummary(
        meeting_id=meeting_id,
        state=cast(Any, state),
        windows=2,
        resumed_from=0,
        ingested=2,
        facts=3,
        completed=completed,
        succeeded=completed and stored,
        stored=stored,
    )


class IngestionHandlerDouble:
    def __init__(self, record_dir: Path, log: list[str]) -> None:
        self.record_dir = record_dir
        self.log = log
        self.calls: list[tuple[IngestionJob, OperatorAccess]] = []
        self.failure: Exception | None = None
        self.report: FakeYouError | None = None
        self.release: asyncio.Event | None = None

    async def __call__(
        self,
        job: IngestionJob,
        *,
        access: OperatorAccess,
        ports: IngestPorts,
    ) -> IngestSummary:
        self.calls.append((job, access))
        if self.release is not None:
            await self.release.wait()
        if self.failure is not None:
            raise self.failure
        if self.report is not None:
            ports.report(self.report)
            return _summary(job.meeting_id, state="failed", completed=False, stored=False)
        path = MeetingDir(self.record_dir, SessionId(job.meeting_id)).ingestion
        write_status(
            path,
            IngestionStatus(
                meeting_id=job.meeting_id,
                group_id="g",
                status="completed",
                settled_job=SettledJob(id=job.job_id, outcome="completed"),
            ),
        )
        self.log.append("status")
        return _summary(job.meeting_id, state="completed", completed=True, stored=True)


class IngestionHarness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = FakeClient()
        self.transport = StreamTransport(cast(Redis, self.client), stream_max_len=1000)
        self.status = MemorySessionStatus()
        self.store = SessionStore(self.status)
        self.handler = IngestionHandlerDouble(settings.paths.record_dir, self.client.log)
        self.shutdown = asyncio.Event()
        self.jobs_queue = build_job_queue(InMemoryConnector())
        self.consumer = self.build()

    def build(self, **overrides: Any) -> IngestionWorker:
        values: dict[str, Any] = {
            "settings": self.settings,
            "transport": self.transport,
            "store": self.store,
            "open_memory": unused_memory,
            "shutdown": self.shutdown,
            "handler": self.handler,
            **overrides,
        }
        return IngestionWorker(
            IngestionDeps(**values),
            self.jobs_queue,
            retry=RetryStrategy(max_attempts=INGESTION_DELIVERY_LIMIT - 1),
        )

    def seed(self, meeting_id: str) -> PersonaProfile:
        persona = PersonaProfile.model_validate(PERSONA_DATA)
        create = SessionCreate(
            link=LINK,
            owner_id="owner-1",
            twin_id="twin-1",
            audio=True,
            conversation=True,
            persona=persona,
        )
        snapshot = build_snapshot(create, meeting_id=meeting_id, at=AT)
        self.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)
        return persona

    def persist(self, meeting_id: str, state: str) -> None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        write_status(
            path, IngestionStatus(meeting_id=meeting_id, group_id="g", status=cast(Any, state))
        )

    async def submit(self, meeting: str, kind: str = "ingest", **overrides: str) -> str:
        await self.jobs_queue.open()
        task = self.jobs_queue.app.configure_task(
            INGEST_TASK, queue=INGEST_QUEUE, lock=f"ingest:{meeting}"
        )
        job_id = await task.defer_async(meeting_id=overrides.get("meeting_id", meeting), kind=kind)
        return str(job_id)

    async def pending(self) -> list[QueuedJob]:
        queued = await self.jobs_queue.jobs_for(INGEST_TASK)
        return [job for job in queued if job.status in {"todo", "doing"}]

    def recorded(self, meeting_id: str) -> tuple[str, str | None, SettledJob | None] | None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        status = read_status(path)
        return None if status is None else (status.status, status.reason, status.settled_job)

    async def jobs(self) -> list[QueuedJob]:
        return await self.jobs_queue.jobs_for(INGEST_TASK)

    async def make_due(self) -> None:
        connector = cast(InMemoryConnector, self.jobs_queue.app.connector)
        for job in connector.jobs.values():
            job["scheduled_at"] = None


@pytest.fixture
def ingestion(settings: Settings) -> IngestionHarness:
    return IngestionHarness(settings)


async def test_ingestion_job_succeeds_once_its_status_is_persisted(
    ingestion: IngestionHarness,
) -> None:
    persona = ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")

    await ingestion.consumer.run_once()

    assert ingestion.client.log == ["status"]
    assert await ingestion.pending() == []
    [(job, access)] = ingestion.handler.calls
    assert job == IngestionJob(meeting_id="m-1", kind="ingest", job_id=entry_id)
    assert access.identity.owner_id == "owner-1"
    assert access.identity.twin_id == "twin-1"
    assert access.identity.persona_path is not None
    assert PersonaProfile.model_validate_json(access.identity.persona_path.read_bytes()) == persona
    assert ingestion.client.events == []


async def test_unstored_summary_is_retried_then_dead_lettered(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")
    ingestion.handler.report = FakeYouError(
        ErrorCode.INGESTION_STATUS_CORRUPT, path="ingestion.json", reason="bad json"
    )

    await ingestion.consumer.run_once()

    assert ingestion.client.log == []
    assert await ingestion.pending() == []
    fetched = await ingestion.jobs_queue.fetch(int(entry_id))
    assert fetched is not None
    assert fetched.status == "failed"
    recorded = ingestion.recorded("m-1")
    assert recorded is not None
    assert recorded[0:2] == ("failed", DEAD_LETTER_DETAIL)


async def test_terminal_unsuccessful_summary_settles_without_reading_as_success(
    ingestion: IngestionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")

    async def failed_ingest(
        request: IngestRequest, *, access: OperatorAccess, ports: IngestPorts
    ) -> bool:
        del access
        ingestion.persist(str(request.meeting_id), "failed")
        ingestion.client.log.append("status")
        ports.publish(
            _summary(str(request.meeting_id), state="failed", completed=False, stored=True)
        )
        return False

    monkeypatch.setattr("fakeyou.memory.ingest_job.run_recorded_ingest", failed_ingest)
    consumer = ingestion.build(handler=handle_ingestion_job)

    await consumer.run_once()

    assert ingestion.client.log == ["status"]
    assert await ingestion.pending() == []
    fetched = await ingestion.jobs_queue.fetch(int(entry_id))
    assert fetched is not None
    assert fetched.status == "succeeded"
    assert ingestion.recorded("m-1") == ("failed", None, None)
    assert ingestion.client.events == []


async def test_invalid_job_is_dropped_and_faults_its_meeting(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1", kind="reembed")

    await ingestion.consumer.run_once()

    assert ingestion.handler.calls == []
    assert await ingestion.pending() == []
    [fault] = ingestion.client.events
    assert fault["type"] == "fault"
    assert fault["meeting_id"] == "m-1"
    assert fault["source"] == "ingestion"
    assert fault["detail"] == INVALID_JOB_DETAIL
    assert ingestion.recorded("m-1") == (
        "failed",
        INVALID_JOB_DETAIL,
        SettledJob(id=entry_id, outcome="failed"),
    )


async def test_invalid_job_without_a_readable_meeting_publishes_no_event(
    ingestion: IngestionHarness,
) -> None:
    await ingestion.submit("m-1", meeting_id="not a meeting/../id")

    await ingestion.consumer.run_once()

    assert ingestion.client.events == []
    assert await ingestion.pending() == []


async def test_retries_are_capped_then_dead_lettered_with_a_terminal_fault(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")
    ingestion.handler.failure = RuntimeError("extraction exploded")

    await ingestion.consumer.run_once()

    assert len(ingestion.handler.calls) == INGESTION_DELIVERY_LIMIT
    assert await ingestion.pending() == []
    assert ingestion.client.log == []
    details = [event["detail"] for event in ingestion.client.events]
    assert details == [FAILED_JOB_DETAIL] * INGESTION_DELIVERY_LIMIT + [DEAD_LETTER_DETAIL]
    assert ingestion.client.events[0]["error_type"] == "RuntimeError"
    assert "exploded" not in str(ingestion.client.events)
    assert ingestion.client.events[-1]["deliveries"] == str(INGESTION_DELIVERY_LIMIT)
    assert ingestion.recorded("m-1") == (
        "failed",
        DEAD_LETTER_DETAIL,
        SettledJob(id=entry_id, outcome="failed"),
    )


async def test_a_retry_keeps_the_previous_status_until_the_run_lands(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    path = MeetingDir(ingestion.settings.paths.record_dir, SessionId("m-1")).ingestion
    before = IngestionStatus(
        meeting_id="m-1",
        group_id="g",
        status="failed",
        episode_uuids={0: "uuid-a"},
        settled_job=SettledJob(id="0-1", outcome="failed"),
    )
    write_status(path, before)
    entry_id = await ingestion.submit("m-1")
    seen_during_retries: list[IngestionStatus | None] = []

    async def flaky(
        job: IngestionJob, *, access: OperatorAccess, ports: IngestPorts
    ) -> IngestSummary:
        if not seen_during_retries:
            seen_during_retries.append(read_status(path))
            raise RuntimeError("extraction exploded")
        return await ingestion.handler(job, access=access, ports=ports)

    ingestion.consumer = ingestion.build(handler=flaky)

    await ingestion.consumer.run_once()

    assert seen_during_retries == [before]
    assert ingestion.recorded("m-1") == (
        "completed",
        None,
        SettledJob(id=entry_id, outcome="completed"),
    )


async def test_dead_letter_waits_for_a_running_job_to_release_the_record(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")
    ingestion.handler.failure = RuntimeError("extraction exploded")
    record = MeetingDir(ingestion.settings.paths.record_dir, SessionId("m-1")).record

    with record_lock(record, purpose="ingestion"):
        await ingestion.consumer.run_once()
        assert ingestion.recorded("m-1") is None
        assert [job.id for job in await ingestion.pending()] != [int(entry_id)]

    await ingestion.make_due()
    await ingestion.consumer.run_once()

    assert await ingestion.pending() == []
    recorded = ingestion.recorded("m-1")
    assert recorded is not None
    assert recorded[0:2] == ("failed", DEAD_LETTER_DETAIL)


async def test_failure_of_a_meeting_without_status_or_session_writes_nothing(
    ingestion: IngestionHarness,
) -> None:
    await ingestion.submit("m-1", kind="reembed")

    await ingestion.consumer.run_once()

    assert ingestion.recorded("m-1") is None


async def test_reported_errors_become_named_fault_events(ingestion: IngestionHarness) -> None:
    ingestion.seed("m-1")
    await ingestion.submit("m-1")
    ingestion.handler.report = FakeYouError(
        ErrorCode.INGESTION_STATUS_CORRUPT, path="ingestion.json", reason="bad json"
    )

    await ingestion.consumer.run_once()

    assert [event["detail"] for event in ingestion.client.events] == [
        "ingestion.status_corrupt",
        FAILED_JOB_DETAIL,
    ] * INGESTION_DELIVERY_LIMIT + [DEAD_LETTER_DETAIL]


async def test_start_requeues_one_ingest_job_per_pending_meeting_without_a_queued_job(
    ingestion: IngestionHarness,
) -> None:
    for meeting_id, state in (
        ("m-1", "pending"),
        ("m-2", "pending"),
        ("m-3", "pending"),
        ("m-4", "completed"),
    ):
        ingestion.persist(meeting_id, state)
    await ingestion.submit("m-3")
    await ingestion.submit("m-2", kind="correct")
    ingestion.shutdown.set()

    await ingestion.consumer.run_forever()

    queued = {(job.args["meeting_id"], job.args["kind"]) for job in await ingestion.jobs()}
    assert queued == {("m-1", "ingest"), ("m-2", "correct"), ("m-3", "ingest")}


async def test_ingestion_job_takes_the_keys_staged_for_its_meeting(settings: Settings) -> None:
    ingestion = IngestionHarness(settings)
    await ingestion.transport.store_overrides("m-1", {"reply": MARKER_REPLY_KEY}, ttl_seconds=120.0)
    consumer = ingestion.build()
    await ingestion.submit("m-1")

    await consumer.run_once()

    [(_job, access)] = ingestion.handler.calls
    session_reply = access.settings.reply.api_key
    assert session_reply is not None
    assert session_reply.get_secret_value() == MARKER_REPLY_KEY
    process_reply = settings.reply.api_key
    assert process_reply is not None
    assert process_reply.get_secret_value() == "test-key"


async def test_ingestion_job_for_a_session_started_with_keys_fails_without_them(
    settings: Settings,
) -> None:
    ingestion = IngestionHarness(settings)
    create = SessionCreate(link=LINK, owner_id="owner-1", twin_id="twin-1")
    snapshot = build_snapshot(create, meeting_id="m-2", at=AT, keys_supplied=True)
    ingestion.status.seed(snapshot.model_dump(mode="json"), STATUS_AT)
    consumer = ingestion.build()
    entry_id = await ingestion.submit("m-2")

    await consumer.run_once()

    assert ingestion.handler.calls == []
    fetched = await ingestion.jobs_queue.fetch(int(entry_id))
    assert fetched is not None
    assert fetched.status == "succeeded"
    recorded = read_status(MeetingDir(settings.paths.record_dir, SessionId("m-2")).ingestion)
    assert recorded is not None
    assert recorded.status == "failed"
    assert recorded.reason == "worker.job_keys_missing"
    assert recorded.settled_job == SettledJob(id=entry_id, outcome="failed")


class _Forever:
    def __init__(self) -> None:
        self.cancelled = False

    async def run_forever(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _Finishes:
    async def run_forever(self) -> None:
        await asyncio.sleep(0)


async def test_worker_run_returns_once_every_consumer_has_returned() -> None:
    await asyncio.wait_for(run_consumers(_Finishes(), _Finishes()), timeout=5)


async def test_worker_refuses_to_boot_when_memory_is_unreachable(settings: Settings) -> None:
    @asynccontextmanager
    async def unreachable(_settings: Settings) -> AsyncIterator[Any]:
        raise FakeYouError(ErrorCode.MEMORY_UNAVAILABLE, reason="neo4j refused the connection")
        yield

    ports = JoinPorts(
        run_session=run_meeting_session,
        open_memory=unreachable,
    )

    with pytest.raises(FakeYouError, match="memory unavailable") as caught:
        await verify_boot_dependencies(settings, ports)

    assert caught.value.code == ErrorCode.MEMORY_UNAVAILABLE


async def test_settling_a_session_queues_the_pending_ingestion_job(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    harness.persist("m-1", "pending")
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    jobs = await harness.jobs.jobs_for("ingest_meeting")
    assert [job.args for job in jobs] == [{"meeting_id": "m-1", "kind": "ingest"}]


async def test_settling_a_session_without_a_pending_ingestion_queues_nothing(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    assert await harness.jobs.jobs_for("ingest_meeting") == []


async def test_settling_a_session_with_an_ingested_meeting_queues_nothing(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    harness.persist("m-1", "completed")
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    assert await harness.jobs.jobs_for("ingest_meeting") == []


async def test_the_debrief_is_queued_when_the_meeting_ends(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    record = MeetingDir(harness.settings.paths.record_dir, SessionId("m-1")).record
    record.write_text('{"kind": "header", "at": "2026-01-01T00:00:00+00:00"}\n')

    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    jobs = await harness.jobs.jobs_for("debrief_meeting")
    assert [job.args for job in jobs] == [{"meeting_id": "m-1", "force": False}]


async def test_no_debrief_is_queued_without_a_record(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"

    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert await harness.jobs.jobs_for("debrief_meeting") == []


async def test_the_terminal_reason_survives_a_live_phase_that_already_ended(
    harness: Harness,
) -> None:
    harness.double.live_phases = ["active", "leaving", "ended"]
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"

    await harness.finish_session(phase="ended", terminal_reason="left_by_participant")

    status = await harness.status.get("m-1")
    assert status is not None
    assert (status.phase, status.terminal_reason, status.detail) == (
        "ended",
        "left_by_participant",
        "harness",
    )
    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]


async def test_a_stop_before_the_twin_joined_ends_the_session(harness: Harness) -> None:
    await harness.seed("m-2")
    await harness.status.desire("m-2", "stopped", STATUS_AT)
    await harness.enqueue("m-2", "stop")

    assert await harness.consumer.run_once() is None

    status = await harness.status.get("m-2")
    assert status is not None
    assert (status.phase, status.terminal_reason) == ("ended", "stopped_by_owner")
    assert harness.runs == []
    assert harness.status.leases == {}


async def test_an_unreachable_database_leaves_the_join_for_later(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unreachable(*_args: object) -> None:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)

    monkeypatch.setattr(harness.status, "claim", unreachable)
    await harness.enqueue("m-1", "join")

    assert await harness.consumer.run_once() is None
    assert harness.runs == []


async def test_a_lost_lease_stops_the_session_and_leaves_the_status_to_the_new_owner(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.double.started.wait()
    harness.status.leases["m-1"] = "worker-b"

    await asyncio.wait_for(harness.stop_event.wait(), timeout=1)
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    assert await harness.phase("m-1") == "joining"
    assert "session_ended" not in [event["type"] for event in harness.events]
    assert harness.status.leases == {"m-1": "worker-b"}


async def test_recovery_gives_up_after_the_attempt_budget(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    row = harness.status.rows["m-1"]
    harness.status.rows["m-1"] = replace(row, attempts=SESSION_MAX_ATTEMPTS)

    assert await harness.consumer.run_once() is None

    status = await harness.status.get("m-1")
    assert status is not None
    assert (status.phase, status.terminal_reason) == ("failed", "infra_failure")
    assert status.detail == f"recovery gave up after {SESSION_MAX_ATTEMPTS} attempts"
    assert harness.runs == []


async def test_a_session_without_its_snapshot_fails_instead_of_looping(harness: Harness) -> None:
    await harness.status.create("m-9", "owner-1", STATUS_AT)
    await harness.status.desire("m-9", "joined", STATUS_AT)
    await harness.status.desire("m-1", "created", STATUS_AT)

    assert await harness.consumer.run_once() is None

    status = await harness.status.get("m-9")
    assert status is not None
    assert (status.phase, status.detail) == ("failed", "the session snapshot is missing")


async def test_a_crash_keeps_the_lease_until_it_expires(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.crash_session()

    assert await harness.other_consumer.run_once() is None
    harness.status.expired.add("m-1")
    assert await harness.other_consumer.run_once() == "m-1"

    assert harness.runs == [("m-1", False), ("m-1", True)]


async def test_a_takeover_that_finds_the_record_locked_waits_instead_of_failing(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.double.started.wait()
    harness.status.expired.add("m-1")

    assert await harness.other_consumer.run_once() is None

    assert await harness.phase("m-1") != "failed"
    assert harness.status.leases == {"m-1": "worker-b"}
    assert harness.runs == [("m-1", False)]


async def test_an_unwritten_terminal_phase_keeps_the_lease(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.double.started.wait()
    original = harness.status.record_phase

    async def lose_terminal(
        meeting_id: str, change: PhaseUpdate, *, attempt_id: str | None = None
    ) -> SessionStatus | None:
        if change.phase in ("ended", "failed"):
            return None
        return await original(meeting_id, change, attempt_id=attempt_id)

    monkeypatch.setattr(harness.status, "record_phase", lose_terminal)
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    assert harness.status.leases == {"m-1": "worker-a"}


async def test_a_hanging_renewal_gives_the_session_up_before_the_lease_runs_out(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("fakeyou.worker.consumer.SESSION_LEASE_SECONDS", 5.05)
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once() == "m-1"
    await harness.double.started.wait()

    async def hang(*_args: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(harness.status, "renew", hang)

    await asyncio.wait_for(harness.stop_event.wait(), timeout=2)


async def test_an_active_session_starts_a_fresh_attempt_budget(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    harness.status.rows["m-1"] = replace(harness.status.rows["m-1"], attempts=4)
    assert await harness.consumer.run_once() == "m-1"
    harness.double.live_phases = ["active"]

    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    status = await harness.status.get("m-1")
    assert status is not None
    assert (status.phase, status.attempts) == ("ended", 0)


async def test_a_dead_lettered_correction_fails_its_job_but_keeps_the_meeting_ingested(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    path = MeetingDir(ingestion.settings.paths.record_dir, SessionId("m-1")).ingestion
    write_status(
        path,
        IngestionStatus(
            meeting_id="m-1", group_id="g", status="completed", episode_uuids={0: "uuid-a"}
        ),
    )
    entry_id = await ingestion.submit("m-1", kind="correct")
    ingestion.handler.failure = RuntimeError("correction exploded")
    for _ in range(INGESTION_DELIVERY_LIMIT + 1):
        await ingestion.consumer.run_once()
        ingestion.client.age(30_000)

    recorded = read_status(path)

    assert recorded is not None
    assert (recorded.status, recorded.episode_uuids) == ("completed", {0: "uuid-a"})
    assert recorded.settled_job == SettledJob(id=entry_id, outcome="failed")
