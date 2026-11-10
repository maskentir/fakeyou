from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from fakeyou.catalog.config import ConfigKey
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.transport.schemas import event_stream
from fakeyou.transport.streams import StreamTransport

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


def recording_transport(*, stream_max_len: int = 10000) -> tuple[StreamTransport, RecordingClient]:
    client = RecordingClient()
    transport = StreamTransport(cast(Redis, client), stream_max_len=stream_max_len)
    return transport, client


def fake_transport(
    *, stream_max_len: int = 10000
) -> tuple[StreamTransport, list[tuple[object, ...]]]:
    transport, client = recording_transport(stream_max_len=stream_max_len)
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


async def test_aclose_closes_the_underlying_client() -> None:
    transport, client = recording_transport()
    await transport.aclose()
    assert client.calls == [("aclose",)]


def test_from_settings_requires_the_configured_redis_url(settings: Settings) -> None:
    with pytest.raises(FakeYouError, match=ConfigKey.TRANSPORT_REDIS_URL) as caught:
        StreamTransport.from_settings(settings)
    assert caught.value.code is ErrorCode.CONFIG_SECRET_MISSING
