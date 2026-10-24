import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from fakeyou.api.schemas import BriefDocument, SessionCreate, SessionSnapshot, build_snapshot
from fakeyou.api.sessions import SessionStore
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.conversation.session import SessionConfig, SessionResult
from fakeyou.conversation.state import PublicPhase
from fakeyou.foundation.taxonomy import TerminalReason
from fakeyou.memory.ingest_job import IngestionJob, handle_ingestion_job
from fakeyou.memory.ingest_service import IngestPorts, IngestRequest
from fakeyou.memory.ingest_status import IngestSummary
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.persona import PersonaProfile
from fakeyou.orchestration.join_types import JoinOutcome, JoinPorts, JoinRequest
from fakeyou.orchestration.runtime import SessionWiring, run_meeting_session
from fakeyou.persistence.brief import MeetingBrief
from fakeyou.persistence.ingestion_status import IngestionStatus, read_status, write_status
from fakeyou.persistence.layout import MeetingDir, meeting_profile_dir
from fakeyou.transport.schemas import (
    DEBRIEF_STREAM,
    INGESTION_STREAM,
    encode_command,
    encode_ingestion_job,
)
from fakeyou.transport.streams import StreamTransport
from fakeyou.worker.__main__ import run_consumers, verify_boot_dependencies
from fakeyou.worker.consumer import (
    WorkerConsumer,
    WorkerDeps,
    join_request_for,
)
from fakeyou.worker.ingestion import (
    DEAD_LETTER_DETAIL,
    FAILED_JOB_DETAIL,
    INGESTION_DELIVERY_LIMIT,
    INGESTION_GROUP,
    INVALID_JOB_DETAIL,
    IngestionConsumer,
    IngestionDeps,
)

AT = "2026-10-16T09:00:00+00:00"
LINK = "https://meet.google.com/abc-defg-hij"
MARKER_REPLY_KEY = "marker-reply-731"
PERSONA_DATA = {
    "owner_id": "owner-1",
    "twin_id": "twin-1",
    "display_name": "Demo Twin",
    "role": "Synthetic project lead",
    "verified_facts": ["The synthetic demo is booked"],
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
        self.stop_event: asyncio.Event = asyncio.Event()
        self.outcome = SessionResult(
            phase="ended", terminal_reason="stopped_by_owner", detail="harness"
        )
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.crash = False

    async def run(self, config: SessionConfig, wiring: SessionWiring) -> SessionResult:
        if wiring.stop is not None:
            self.stop_event = wiring.stop
        self.runs.append((str(config.meeting_id), bool(config.resume)))
        self.started.set()
        await self.release.wait()
        self.started.clear()
        self.release.clear()
        if self.crash:
            self.crash = False
            raise OSError("session crashed")
        return self.outcome


def unused_memory(_settings: Settings) -> Any:
    pytest.fail("memory must stay closed")


def unused_checkpointer(*_args: object, **_kwargs: object) -> Any:
    pytest.fail("checkpointer must stay closed")


class SnapshotReader(Protocol):
    def load(self, meeting_id: str) -> SessionSnapshot: ...


class Harness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = FakeClient()
        self.transport = StreamTransport(
            cast(Redis, self.client), stream_max_len=1000, lease_seconds=30.0
        )
        self.discovery = self.transport
        self.session_store = SessionStore(settings.paths.record_dir)
        self.store: SnapshotReader = cast(SnapshotReader, self.session_store)
        self.double = SessionDouble()
        self.ports = JoinPorts(
            run_session=self.double.run,
            open_memory=unused_memory,
            open_checkpointer=unused_checkpointer,
        )
        self.consumer = self.member(settings, "worker-a")
        self.other_consumer = self.member(settings, "worker-b")

    def member(self, settings: Settings, worker_id: str) -> WorkerConsumer:
        return WorkerConsumer(
            WorkerDeps(
                settings=settings,
                transport=self.transport,
                store=self.session_store,
                ports=self.ports,
                shutdown=asyncio.Event(),
                worker_id=worker_id,
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

    def seed(self, meeting_id: str) -> None:
        snapshot = build_snapshot(
            SessionCreate(link=LINK, owner_id="owner-1", twin_id="twin-1"),
            meeting_id=meeting_id,
            at=AT,
        )
        self.session_store.save_new(snapshot)

    async def enqueue(self, meeting_id: str, kind: str) -> None:
        if kind == "join":
            self.session_store.set_phase(meeting_id, phase="joining", terminal_reason=None)
        await self.transport.publish_command(
            meeting_id, encode_command(kind, meeting_id=meeting_id, at=AT)
        )

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


@pytest.fixture
async def harness(settings: Settings) -> AsyncIterator[Harness]:
    harness = Harness(settings)
    harness.seed("m-1")
    try:
        yield harness
    finally:
        await harness.aclose()


async def test_one_join_runs_one_session_and_acks_after_phase_persisted(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert harness.runs == [("m-1", False)]
    assert [kind for kind, _ in harness.acks] == ["join"]
    assert harness.store.load("m-1").phase == "ended"
    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]


async def test_a_join_that_fails_before_the_session_starts_settles_as_failed(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail_before_binding(*_args: object) -> JoinOutcome:
        raise FakeYouError(ErrorCode.MEETING_PERSONA_LANGUAGE_MISMATCH)

    monkeypatch.setattr("fakeyou.worker.consumer.run_join_session", fail_before_binding)
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) is None
    assert harness.store.load("m-1").phase == "failed"
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
    assert await harness.transport.acquire_lease("m-1", "after-failure")


async def test_second_worker_cannot_claim_the_same_meeting(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    assert await harness.other_consumer.run_once(harness.discovery) is None
    assert harness.runs == [("m-1", False)]


async def test_stop_command_sets_the_session_stop_event(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    await harness.consumer.run_once(harness.discovery)
    await harness.enqueue("m-1", "stop")
    await harness.consumer.poll_commands()
    assert harness.stop_event.is_set()
    assert harness.store.load("m-1").phase == "leaving"


async def test_resume_command_continues_the_same_meeting_after_a_crash(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    await harness.crash_session()
    await harness.enqueue("m-1", "resume")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    assert harness.runs == [("m-1", False), ("m-1", True)]
    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert harness.store.load("m-1").phase == "ended"
    assert [event["type"] for event in harness.events] == ["phase_changed", "session_ended"]


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

    assert await harness.consumer.run_once(harness.discovery) == "m-1"
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

    assert await harness.consumer.run_once(harness.discovery) == "m-1"
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
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    await harness.crash_session()
    await harness.enqueue("m-1", "resume")

    assert await harness.consumer.run_once(harness.discovery) == "m-1"
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
        self.acks = True
        self.failure: Exception | None = None
        self.report: FakeYouError | None = None
        self.release: asyncio.Event | None = None

    async def __call__(
        self,
        job: IngestionJob,
        *,
        access: OperatorAccess,
        ports: IngestPorts,
        ack: Callable[[], Awaitable[None]],
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
            path, IngestionStatus(meeting_id=job.meeting_id, group_id="g", status="completed")
        )
        self.log.append("status")
        summary = _summary(job.meeting_id, state="completed", completed=True, stored=True)
        if self.acks:
            await ack()
        return summary


class IngestionHarness:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = FakeClient()
        self.transport = StreamTransport(
            cast(Redis, self.client), stream_max_len=1000, lease_seconds=30.0
        )
        self.store = SessionStore(settings.paths.record_dir)
        self.handler = IngestionHandlerDouble(settings.paths.record_dir, self.client.log)
        self.shutdown = asyncio.Event()
        self.consumer = self.build()

    def build(self, **overrides: Any) -> IngestionConsumer:
        values: dict[str, Any] = {
            "settings": self.settings,
            "transport": self.transport,
            "store": self.store,
            "open_memory": unused_memory,
            "shutdown": self.shutdown,
            "worker_id": "worker-a",
            "handler": self.handler,
            "reconnect_pause_seconds": 0.0,
            **overrides,
        }
        return IngestionConsumer(IngestionDeps(**values))

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
        self.store.save_new(build_snapshot(create, meeting_id=meeting_id, at=AT))
        return persona

    def persist(self, meeting_id: str, state: str) -> None:
        path = MeetingDir(self.settings.paths.record_dir, SessionId(meeting_id)).ingestion
        write_status(
            path, IngestionStatus(meeting_id=meeting_id, group_id="g", status=cast(Any, state))
        )

    async def submit(self, meeting: str, kind: str = "ingest", **overrides: str) -> str:
        fields = {**encode_ingestion_job(meeting_id=meeting, kind="ingest", at=AT), **overrides}
        fields["kind"] = kind
        return await self.transport.publish_job(INGESTION_STREAM, fields)

    def pending(self) -> list[PendingEntry]:
        return self.client.pending_for(INGESTION_STREAM, INGESTION_GROUP)

    def jobs(self) -> list[dict[str, str]]:
        return [fields for _entry_id, fields in self.client.streams.get(INGESTION_STREAM, [])]


@pytest.fixture
def ingestion(settings: Settings) -> IngestionHarness:
    return IngestionHarness(settings)


async def test_ingestion_job_is_acked_only_by_the_handler_after_its_status(
    ingestion: IngestionHarness,
) -> None:
    persona = ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")

    summary = await ingestion.consumer.run_once()

    assert summary is not None
    assert summary.succeeded is True
    assert ingestion.client.log == ["status", f"ack:{entry_id}"]
    assert ingestion.pending() == []
    [(job, access)] = ingestion.handler.calls
    assert job == IngestionJob(meeting_id="m-1", kind="ingest")
    assert access.identity.owner_id == "owner-1"
    assert access.identity.twin_id == "twin-1"
    assert access.identity.persona_path is not None
    assert PersonaProfile.model_validate_json(access.identity.persona_path.read_bytes()) == persona
    assert ingestion.client.events == []


async def test_handler_that_does_not_ack_leaves_the_entry_pending(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")
    ingestion.handler.acks = False

    await ingestion.consumer.run_once()

    assert ingestion.client.log == ["status"]
    assert [(entry.entry_id, entry.deliveries) for entry in ingestion.pending()] == [(entry_id, 1)]


async def test_terminal_unsuccessful_summary_is_acked_without_reading_as_success(
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

    summary = await consumer.run_once()

    assert summary is not None
    assert summary.state == "failed"
    assert summary.succeeded is False
    assert ingestion.client.log == ["status", f"ack:{entry_id}"]
    assert ingestion.pending() == []
    assert ingestion.client.events == []


async def test_invalid_entry_is_not_acked_and_faults_its_meeting(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1", kind="reembed")

    assert await ingestion.consumer.run_once() is None

    assert ingestion.handler.calls == []
    assert [entry.entry_id for entry in ingestion.pending()] == [entry_id]
    assert ingestion.client.acks == []
    [fault] = ingestion.client.events
    assert fault["type"] == "fault"
    assert fault["meeting_id"] == "m-1"
    assert fault["source"] == "ingestion"
    assert fault["detail"] == INVALID_JOB_DETAIL


async def test_invalid_entry_without_a_readable_meeting_publishes_no_event(
    ingestion: IngestionHarness,
) -> None:
    entry_id = await ingestion.submit("m-1", meeting_id="not a meeting/../id")

    assert await ingestion.consumer.run_once() is None

    assert ingestion.client.events == []
    assert [entry.entry_id for entry in ingestion.pending()] == [entry_id]


async def test_redelivery_is_capped_then_acked_with_a_terminal_fault(
    ingestion: IngestionHarness,
) -> None:
    ingestion.seed("m-1")
    entry_id = await ingestion.submit("m-1")
    ingestion.handler.failure = RuntimeError("extraction exploded")

    for _ in range(INGESTION_DELIVERY_LIMIT + 1):
        await ingestion.consumer.run_once()
        ingestion.client.age(30_000)
    assert await ingestion.consumer.run_once() is None

    assert len(ingestion.handler.calls) == INGESTION_DELIVERY_LIMIT
    assert ingestion.pending() == []
    assert ingestion.client.log == [f"ack:{entry_id}"]
    details = [event["detail"] for event in ingestion.client.events]
    assert details == [FAILED_JOB_DETAIL] * INGESTION_DELIVERY_LIMIT + [DEAD_LETTER_DETAIL]
    assert ingestion.client.events[0]["error_type"] == "RuntimeError"
    assert "exploded" not in str(ingestion.client.events)
    assert ingestion.client.events[-1]["deliveries"] == str(INGESTION_DELIVERY_LIMIT)


async def test_reported_errors_become_named_fault_events(ingestion: IngestionHarness) -> None:
    ingestion.seed("m-1")
    await ingestion.submit("m-1")
    ingestion.handler.report = FakeYouError(
        ErrorCode.INGESTION_STATUS_CORRUPT, path="ingestion.json", reason="bad json"
    )

    await ingestion.consumer.run_once()

    assert [event["detail"] for event in ingestion.client.events] == ["ingestion.status_corrupt"]
    assert ingestion.client.acks == []


async def test_start_requeues_one_ingest_job_per_pending_meeting_without_unacked_entries(
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
    await ingestion.transport.ensure_group(INGESTION_STREAM, INGESTION_GROUP)
    await ingestion.transport.read_group(INGESTION_STREAM, INGESTION_GROUP, "worker-z")
    await ingestion.submit("m-2", kind="correct")
    ingestion.shutdown.set()

    await ingestion.consumer.run_forever()

    requeued = ingestion.jobs()[2:]
    assert [{key: value for key, value in job.items() if key != "at"} for job in requeued] == [
        {"v": "1", "type": "ingestion_job", "meeting_id": "m-1", "kind": "ingest"}
    ]
    assert datetime.fromisoformat(requeued[0]["at"]).tzinfo is not None


async def test_redis_reconnect_scans_pending_meetings_again(
    ingestion: IngestionHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    scans: list[Path] = []

    def scan(record_root: Path) -> list[str]:
        scans.append(record_root)
        if len(scans) == 2:
            ingestion.shutdown.set()
        return []

    monkeypatch.setattr("fakeyou.worker.ingestion.pending_ingestion_meetings", scan)
    ingestion.client.failing_reads = 1

    await ingestion.consumer.run_forever()

    assert scans == [ingestion.settings.paths.record_dir] * 2


async def test_long_jobs_keep_their_entry_claimed_without_counting_deliveries(
    ingestion: IngestionHarness,
) -> None:
    ingestion.settings.transport.redis.lease_seconds = 0.03
    consumer = ingestion.build()
    ingestion.seed("m-1")
    await ingestion.submit("m-1")
    ingestion.handler.release = asyncio.Event()

    running = asyncio.create_task(consumer.run_once())
    for _ in range(200):
        if any(call[0] == "xclaim" and call[4] is True for call in ingestion.client.calls):
            break
        await asyncio.sleep(0.005)
    ingestion.handler.release.set()
    await running

    assert any(call[0] == "xclaim" and call[4] is True for call in ingestion.client.calls)
    assert ingestion.pending() == []


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
    ingestion.store.save_new(build_snapshot(create, meeting_id="m-2", at=AT, keys_supplied=True))
    consumer = ingestion.build()
    await ingestion.submit("m-2")

    await consumer.run_once()

    assert ingestion.handler.calls == []
    assert ingestion.client.acks != []
    recorded = read_status(MeetingDir(settings.paths.record_dir, SessionId("m-2")).ingestion)
    assert recorded is not None
    assert recorded.status == "failed"
    assert recorded.reason == "worker.job_keys_missing"


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
        open_checkpointer=unused_checkpointer,
    )

    with pytest.raises(FakeYouError, match="memory unavailable") as caught:
        await verify_boot_dependencies(settings, ports)

    assert caught.value.code == ErrorCode.MEMORY_UNAVAILABLE


async def test_settling_a_session_queues_the_pending_ingestion_job(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    harness.persist("m-1", "pending")
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    jobs = harness.client.streams.get(INGESTION_STREAM, [])
    assert [fields.get("kind") for _id, fields in jobs] == ["ingest"]
    assert [fields.get("meeting_id") for _id, fields in jobs] == ["m-1"]
    assert [fields.get("type") for _id, fields in jobs] == ["ingestion_job"]


async def test_settling_a_session_without_a_pending_ingestion_queues_nothing(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    assert harness.client.streams.get(INGESTION_STREAM, []) == []


async def test_settling_a_session_with_an_ingested_meeting_queues_nothing(
    harness: Harness,
) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    harness.persist("m-1", "completed")
    await harness.finish_session(phase="ended", terminal_reason="stopped_by_owner")

    assert harness.client.streams.get(INGESTION_STREAM, []) == []


async def test_the_debrief_is_queued_when_the_meeting_ends(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"
    record = MeetingDir(harness.settings.paths.record_dir, SessionId("m-1")).record
    record.write_text('{"kind": "header", "at": "2026-01-01T00:00:00+00:00"}\n')

    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")

    jobs = [fields for _entry, fields in harness.client.streams.get(DEBRIEF_STREAM, [])]
    assert [job["meeting_id"] for job in jobs] == ["m-1"]
    assert jobs[0]["type"] == "debrief_job"


async def test_no_debrief_is_queued_without_a_record(harness: Harness) -> None:
    await harness.enqueue("m-1", "join")
    assert await harness.consumer.run_once(harness.discovery) == "m-1"

    await harness.finish_session(phase="ended", terminal_reason="left_by_owner")
    assert harness.client.streams.get(DEBRIEF_STREAM, []) == []
