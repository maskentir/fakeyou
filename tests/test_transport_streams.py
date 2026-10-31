from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import cast
from uuid import uuid4

import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.transport.schemas import INGESTION_STREAM, command_stream, event_stream
from fakeyou.transport.streams import PendingDelivery, StreamTransport

AT = "2026-10-16T09:00:00+00:00"


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


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.pending: dict[tuple[str, str], list[tuple[str, dict[str, str]]]] = {}
        self.groups: set[tuple[str, str]] = set()
        self.deliveries: dict[str, int] = {}
        self.last_delivered: dict[tuple[str, str], str] = {}
        self.next_error: ResponseError | None = None

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
        return entry_id

    async def set(
        self, name: str, value: str, *, nx: bool = False, px: int | None = None
    ) -> bool | None:
        self.calls.append(("set", name, value, px))
        if nx and name in self.values:
            return None
        self.values[name] = value
        return True

    async def getdel(self, name: str) -> str | None:
        self.calls.append(("getdel", name))
        return self.values.pop(name, None)

    async def eval(self, script: str, _numkeys: int, *keys_and_args: str) -> int:
        key, owner = keys_and_args[0], keys_and_args[1]
        self.calls.append(("eval", script, key, owner))
        if self.values.get(key) != owner:
            return 0
        if "PEXPIRE" in script:
            return 1
        del self.values[key]
        return 1

    async def xgroup_create(
        self, name: str, groupname: str, *, id: str = "$", mkstream: bool = False
    ) -> bool:
        self.calls.append(("xgroup_create", name, groupname, id, mkstream))
        if self.next_error is not None:
            error, self.next_error = self.next_error, None
            raise error
        if (name, groupname) in self.groups:
            raise ResponseError("BUSYGROUP Consumer Group name already exists")
        self.groups.add((name, groupname))
        return True

    async def xpending_range(
        self, name: str, groupname: str, *range_args: str | int, idle: int | None = None
    ) -> list[dict[str, object]]:
        minimum, maximum, count = range_args
        self.calls.append(("xpending_range", name, groupname, minimum, maximum, count, idle))
        entries = self.pending.get((name, groupname), [])
        return [
            {
                "message_id": entry_id,
                "consumer": "worker-a",
                "time_since_delivered": idle or 0,
                "times_delivered": self.deliveries.get(entry_id, 1),
            }
            for entry_id, _fields in entries[: int(count)]
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
        self.calls.append(("xautoclaim", name, groupname, consumername, min_idle_time, count))
        entries = self.pending.get((name, groupname), [])
        return ["0-0", [*entries[:count], (None, None)], []]

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
        call: tuple[object, ...] = ("xclaim", name, groupname, consumername, min_idle_time)
        self.calls.append((*call, message_ids, "justid") if justid else (*call, message_ids))
        entries = dict(self.pending.get((name, groupname), []))
        if justid:
            return [entry_id for entry_id in message_ids if entry_id in entries]
        return [(entry_id, entries[entry_id]) for entry_id in message_ids if entry_id in entries]

    async def xack(self, name: str, groupname: str, *entry_ids: str) -> int:
        self.calls.append(("xack", name, groupname, *entry_ids))
        return len(entry_ids)

    async def xinfo_groups(self, name: str) -> list[dict[str, object]]:
        self.calls.append(("xinfo_groups", name))
        return [
            {
                "name": groupname,
                "pending": len(self.pending.get((stream, groupname), [])),
                "last-delivered-id": self.last_delivered.get((stream, groupname), "0-0"),
            }
            for stream, groupname in sorted(self.groups)
            if stream == name
        ]

    async def xrange(
        self, name: str, min: str = "-", max: str = "+", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        self.calls.append(("xrange", name, min, max, count))
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


def recording_transport(
    *, stream_max_len: int = 10000, lease_seconds: float = 30.0
) -> tuple[StreamTransport, RecordingClient]:
    client = RecordingClient()
    transport = StreamTransport(
        cast(Redis, client), stream_max_len=stream_max_len, lease_seconds=lease_seconds
    )
    return transport, client


def fake_transport(
    *, stream_max_len: int = 10000, lease_seconds: float = 30.0
) -> tuple[StreamTransport, list[tuple[object, ...]]]:
    transport, client = recording_transport(
        stream_max_len=stream_max_len, lease_seconds=lease_seconds
    )
    return transport, client.calls


async def test_overrides_are_stored_with_a_ttl_and_taken_exactly_once() -> None:
    transport, client = recording_transport()

    await transport.store_overrides("m-1", {"reply": "reply-key"}, ttl_seconds=120.0)

    assert (
        "set",
        "fakeyou:overrides:m-1",
        '{"reply": "reply-key"}',
        120000,
    ) in client.calls
    assert await transport.take_overrides("m-1") == {"reply": "reply-key"}
    assert await transport.take_overrides("m-1") == {}


async def test_publish_event_validates_and_trims() -> None:
    transport, calls = fake_transport(stream_max_len=500)
    entry = await transport.publish_event(
        "m-1", {"v": "1", "type": "phase_changed", "at": AT, "meeting_id": "m-1", "phase": "active"}
    )
    assert entry == "1-0"
    assert calls == [("xadd", "fakeyou:meeting:m-1:events", 500, True)]
    with pytest.raises(ValueError, match="event"):
        await transport.publish_event(
            "m-1",
            {"v": "1", "type": "phase_changed", "at": AT, "meeting_id": "m-1", "evidence": "raw"},
        )


async def test_lease_is_atomic_and_owner_checked() -> None:
    transport, _calls = fake_transport()
    assert await transport.acquire_lease("m-1", "worker-a") is True
    assert await transport.acquire_lease("m-1", "worker-b") is False
    assert await transport.renew_lease("m-1", "worker-b") is False
    await transport.release_lease("m-1", "worker-b")
    assert await transport.renew_lease("m-1", "worker-a") is True


async def test_publish_command_and_job_trim_approximately() -> None:
    transport, calls = fake_transport(stream_max_len=7)
    await transport.publish_command(
        "m-2", {"v": "1", "type": "stop", "at": AT, "meeting_id": "m-2"}
    )
    await transport.publish_job(INGESTION_STREAM, {"v": "1", "type": "ingestion_job"})
    assert calls == [
        ("xadd", "fakeyou:meeting:m-2:commands", 7, True),
        ("xadd", "fakeyou:jobs:ingestion", 7, True),
    ]


async def test_ensure_group_swallows_only_busygroup() -> None:
    transport, client = recording_transport()
    stream = "fakeyou:jobs:ingestion"
    await transport.ensure_group(stream, "workers")
    await transport.ensure_group(stream, "workers")
    assert client.calls[-1] == ("xgroup_create", stream, "workers", "0", True)
    client.next_error = ResponseError("LOADING Redis is loading the dataset in memory")
    with pytest.raises(ResponseError, match="LOADING"):
        await transport.ensure_group(stream, "workers")


async def test_claim_stale_reclaims_pending_entries_without_ack() -> None:
    transport, client = recording_transport()
    stream = command_stream("m-3")
    entry = ("2-0", {"v": "1", "type": "join", "at": AT, "meeting_id": "m-3"})
    client.pending[(stream, "workers")] = [entry]
    claimed = await transport.claim_stale(stream, "workers", "worker-b", min_idle_ms=0)
    assert claimed == [entry]
    assert client.calls == [("xautoclaim", stream, "workers", "worker-b", 0, 100)]


async def test_pending_deliveries_report_the_redis_delivery_count() -> None:
    transport, client = recording_transport()
    group = "ingestion-workers"
    client.pending[(INGESTION_STREAM, group)] = [("3-0", {"kind": "ingest"}), ("4-0", {})]
    client.deliveries = {"3-0": 5, "4-0": 2}

    pending = await transport.pending_deliveries(INGESTION_STREAM, group, min_idle_ms=30000)

    assert pending == [PendingDelivery("3-0", 5), PendingDelivery("4-0", 2)]
    assert client.calls == [("xpending_range", INGESTION_STREAM, group, "-", "+", 100, 30000)]


async def test_claim_entry_claims_one_id_and_returns_its_fields() -> None:
    transport, client = recording_transport()
    group = "ingestion-workers"
    client.pending[(INGESTION_STREAM, group)] = [("3-0", {"kind": "ingest"})]

    claimed = await transport.claim_entry(
        INGESTION_STREAM, group, "worker-b", "3-0", min_idle_ms=30000
    )
    missing = await transport.claim_entry(
        INGESTION_STREAM, group, "worker-b", "9-0", min_idle_ms=30000
    )

    assert claimed == {"kind": "ingest"}
    assert missing is None
    assert client.calls[0] == ("xclaim", INGESTION_STREAM, group, "worker-b", 30000, ["3-0"])


async def test_touch_entry_resets_idle_without_counting_a_delivery() -> None:
    transport, client = recording_transport()
    group = "ingestion-workers"
    client.pending[(INGESTION_STREAM, group)] = [("3-0", {"kind": "ingest"})]

    await transport.touch_entry(INGESTION_STREAM, group, "worker-a", "3-0")

    assert client.calls == [("xclaim", INGESTION_STREAM, group, "worker-a", 0, ["3-0"], "justid")]


async def test_unacked_entries_cover_pending_and_undelivered_entries() -> None:
    transport, client = recording_transport()
    group = "ingestion-workers"
    delivered = ("1-0", {"meeting_id": "m-1"})
    acked = ("2-0", {"meeting_id": "m-2"})
    fresh = ("3-0", {"meeting_id": "m-3"})
    client.streams[INGESTION_STREAM] = [delivered, acked, fresh]
    client.groups.add((INGESTION_STREAM, group))
    client.pending[(INGESTION_STREAM, group)] = [delivered]
    client.last_delivered[(INGESTION_STREAM, group)] = "2-0"

    assert await transport.unacked_entries(INGESTION_STREAM, group) == [delivered, fresh]


async def test_entry_ack_closure_acks_exactly_its_entry() -> None:
    transport, client = recording_transport()

    ack = transport.create_entry_ack(INGESTION_STREAM, "ingestion-workers", "3-0")

    assert client.calls == []
    await ack()
    assert client.calls == [("xack", INGESTION_STREAM, "ingestion-workers", "3-0")]


async def test_read_events_is_exclusive_after_the_given_id() -> None:
    transport, client = recording_transport()
    stream = event_stream("m-4")
    client.streams[stream] = [
        ("1-0", {"v": "1", "type": "session_ended", "at": AT, "meeting_id": "m-4"}),
        ("1-1", {"v": "1", "type": "session_ended", "at": AT, "meeting_id": "m-4"}),
    ]
    events = await transport.read_events("m-4", after="1-0", count=50)
    assert [entry_id for entry_id, _fields in events] == ["1-1"]
    assert client.calls == [("xrange", stream, "(1-0", "+", 50)]


async def test_read_entry_returns_only_the_exact_id_or_none() -> None:
    transport, client = recording_transport()
    stream = "fakeyou:jobs:ingestion"
    client.streams[stream] = [
        ("1-0", {"v": "1", "type": "ingestion_job", "kind": "ingest"}),
        ("10-0", {"v": "1", "type": "ingestion_job", "kind": "correct"}),
    ]
    assert await transport.read_entry(stream, "10-0") == {
        "v": "1",
        "type": "ingestion_job",
        "kind": "correct",
    }
    assert await transport.read_entry(stream, "2-0") is None
    assert await transport.read_entry("fakeyou:jobs:debrief", "1-0") is None
    assert client.calls[-1] == ("xrange", "fakeyou:jobs:debrief", "1-0", "1-0", 1)


async def test_aclose_closes_the_underlying_client() -> None:
    transport, client = recording_transport()
    await transport.aclose()
    assert client.calls == [("aclose",)]


async def test_candidate_meetings_lists_only_command_streams() -> None:
    transport, client = recording_transport()
    for stream in (
        command_stream("m-2"),
        command_stream("m-1"),
        event_stream("m-1"),
        INGESTION_STREAM,
    ):
        client.streams.setdefault(stream, [])
    assert await transport.candidate_meetings() == ["m-1", "m-2"]
    assert client.calls == [("scan", 0, "fakeyou:meeting:*:commands", 100)]


def test_from_settings_requires_the_configured_redis_url(settings: Settings) -> None:
    with pytest.raises(FakeYouError, match=ConfigKey.TRANSPORT_REDIS_URL) as caught:
        StreamTransport.from_settings(settings)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING


@pytest.mark.container
async def test_container_transport_reclaims_stale_commands_and_clears_pending(
    settings: Settings, redis_endpoint: str
) -> None:
    settings.transport.redis.url = SecretStr(redis_endpoint)
    settings.transport.redis.stream_max_len = 1000
    transport = StreamTransport.from_settings(settings)
    meeting_id = f"e5-{uuid4().hex[:10]}"
    stream = command_stream(meeting_id)
    fields = {"v": "1", "type": "join", "at": AT, "meeting_id": meeting_id}
    try:
        entry_id = await transport.publish_command(meeting_id, fields)
        await transport.ensure_group(stream, "workers")
        delivered = await transport.read_group(stream, "workers", "worker-a", count=1, block_ms=100)
        assert delivered == [(entry_id, fields)]
        assert (
            await transport.read_group(stream, "workers", "worker-b", count=1, block_ms=100) == []
        )
        claimed = await transport.claim_stale(stream, "workers", "worker-b", min_idle_ms=0)
        assert claimed == [(entry_id, fields)]
        await transport.ack(stream, "workers", entry_id)
        assert await transport.claim_stale(stream, "workers", "worker-b", min_idle_ms=0) == []
    finally:
        await transport.aclose()


@pytest.mark.container
async def test_container_transport_counts_deliveries_and_lists_unacked_jobs(
    settings: Settings, redis_endpoint: str
) -> None:
    settings.transport.redis.url = SecretStr(redis_endpoint)
    transport = StreamTransport.from_settings(settings)
    stream = f"fakeyou:jobs:ingestion-{uuid4().hex[:10]}"
    group = "ingestion-workers"
    first = {"v": "1", "type": "ingestion_job", "at": AT, "meeting_id": "m-1", "kind": "ingest"}
    second = {**first, "meeting_id": "m-2"}
    try:
        first_id = await transport.publish_job(stream, first)
        await transport.ensure_group(stream, group)
        assert await transport.read_group(stream, group, "worker-a", count=1, block_ms=100) == [
            (first_id, first)
        ]
        second_id = await transport.publish_job(stream, second)
        assert await transport.pending_deliveries(stream, group, min_idle_ms=0) == [
            PendingDelivery(first_id, 1)
        ]
        claimed = await transport.claim_entry(stream, group, "worker-b", first_id, min_idle_ms=0)
        assert claimed == first
        await transport.touch_entry(stream, group, "worker-b", first_id)
        assert await transport.pending_deliveries(stream, group, min_idle_ms=0) == [
            PendingDelivery(first_id, 2)
        ]
        assert await transport.unacked_entries(stream, group) == [
            (first_id, first),
            (second_id, second),
        ]
        await transport.create_entry_ack(stream, group, first_id)()
        assert await transport.unacked_entries(stream, group) == [(second_id, second)]
    finally:
        await transport.aclose()
