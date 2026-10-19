import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.typing import EncodableT, FieldT, StreamIdT

from fakeyou.catalog.config import ConfigKey
from fakeyou.config.settings import Settings, require_secret
from fakeyou.transport.schemas import command_stream, event_stream, validate_event

_LEASE_RENEW = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] "
    "then return redis.call('PEXPIRE', KEYS[1], ARGV[2]) "
    "else return 0 end"
)
_LEASE_RELEASE = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] "
    "then return redis.call('DEL', KEYS[1]) "
    "else return 0 end"
)
_CLAIM_BATCH = 100
_SCAN_MATCH = "fakeyou:meeting:*:commands"
OVERRIDE_TTL_SECONDS = 120.0
_SCAN_COUNT = 100
_FIRST_ENTRY_ID = "0-0"
CONNECTION_ERRORS: tuple[type[Exception], ...] = (RedisConnectionError, RedisTimeoutError)


@dataclass(frozen=True, slots=True)
class PendingDelivery:
    entry_id: str
    deliveries: int


def _lease_key(meeting_id: str) -> str:
    return f"fakeyou:meeting:{meeting_id}:lease"


def _overrides_key(scope: str) -> str:
    return f"fakeyou:overrides:{scope}"


def _entry_pairs(entries: Any) -> list[tuple[str, dict[str, str]]]:
    return [(str(entry_id), dict(fields)) for entry_id, fields in entries]


class StreamTransport:
    def __init__(self, client: Redis, *, stream_max_len: int, lease_seconds: float) -> None:
        self._client = client
        self._stream_max_len = stream_max_len
        self._lease_ms = max(1, int(lease_seconds * 1000))

    async def _publish(self, stream: str, fields: Mapping[str, str]) -> str:
        payload: dict[FieldT, EncodableT] = {name: value for name, value in fields.items()}
        return cast(
            str,
            await self._client.xadd(stream, payload, maxlen=self._stream_max_len, approximate=True),
        )

    async def publish_command(self, meeting_id: str, fields: Mapping[str, str]) -> str:
        return await self._publish(command_stream(meeting_id), fields)

    async def publish_event(self, meeting_id: str, fields: Mapping[str, str]) -> str:
        validated = validate_event(fields, meeting_id=meeting_id)
        return await self._publish(event_stream(meeting_id), validated)

    async def publish_job(self, stream: str, fields: Mapping[str, str]) -> str:
        return await self._publish(stream, fields)

    async def store_overrides(
        self, scope: str, overrides: Mapping[str, str], *, ttl_seconds: float
    ) -> None:
        await self._client.set(
            _overrides_key(scope),
            json.dumps(dict(overrides)),
            px=max(1, int(ttl_seconds * 1000)),
        )

    async def take_overrides(self, scope: str) -> dict[str, str]:
        raw = await self._client.getdel(_overrides_key(scope))
        if raw is None:
            return {}
        document: dict[str, str] = json.loads(raw)
        return document

    async def ensure_group(self, stream: str, group: str) -> None:
        try:
            await self._client.xgroup_create(stream, group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 5000,
    ) -> list[tuple[str, dict[str, str]]]:
        response = await self._client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        if not response:
            return []
        return _entry_pairs(response[0][1])

    async def ack(self, stream: str, group: str, entry_id: str) -> None:
        await self._client.xack(stream, group, entry_id)

    async def claim_stale(
        self, stream: str, group: str, consumer: str, *, min_idle_ms: int
    ) -> list[tuple[str, dict[str, str]]]:
        pending = await self._client.xpending_range(
            stream, group, "-", "+", _CLAIM_BATCH, idle=min_idle_ms
        )
        entry_ids: list[StreamIdT] = [str(entry["message_id"]) for entry in pending]
        if not entry_ids:
            return []
        claimed = await self._client.xclaim(stream, group, consumer, min_idle_ms, entry_ids)
        return _entry_pairs(claimed)

    async def pending_deliveries(
        self, stream: str, group: str, *, min_idle_ms: int
    ) -> list[PendingDelivery]:
        pending = await self._client.xpending_range(
            stream, group, "-", "+", _CLAIM_BATCH, idle=min_idle_ms
        )
        return [
            PendingDelivery(str(entry["message_id"]), int(entry["times_delivered"]))
            for entry in pending
        ]

    async def claim_entry(
        self, stream: str, group: str, consumer: str, entry_id: str, *, min_idle_ms: int
    ) -> dict[str, str] | None:
        claimed = await self._client.xclaim(stream, group, consumer, min_idle_ms, [entry_id])
        for found_id, fields in _entry_pairs(claimed):
            if found_id == entry_id:
                return fields
        return None

    async def touch_entry(self, stream: str, group: str, consumer: str, entry_id: str) -> None:
        await self._client.xclaim(stream, group, consumer, 0, [entry_id], justid=True)

    async def unacked_entries(self, stream: str, group: str) -> list[tuple[str, dict[str, str]]]:
        groups = await self._client.xinfo_groups(stream)
        last_delivered = next(
            (str(info["last-delivered-id"]) for info in groups if str(info["name"]) == group),
            _FIRST_ENTRY_ID,
        )
        pending = await self._client.xpending_range(stream, group, "-", "+", self._stream_max_len)
        unacked: list[tuple[str, dict[str, str]]] = []
        for entry in pending:
            entry_id = str(entry["message_id"])
            fields = await self.read_entry(stream, entry_id)
            if fields is not None:
                unacked.append((entry_id, fields))
        undelivered = await self._client.xrange(stream, min=f"({last_delivered}", max="+")
        return unacked + _entry_pairs(undelivered)

    def create_entry_ack(
        self, stream: str, group: str, entry_id: str
    ) -> Callable[[], Awaitable[None]]:
        return partial(self.ack, stream, group, entry_id)

    async def read_events(
        self, meeting_id: str, *, after: str, count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        entries = await self._client.xrange(
            event_stream(meeting_id), min=f"({after}", max="+", count=count
        )
        return _entry_pairs(entries)

    async def read_entry(self, stream: str, entry_id: str) -> dict[str, str] | None:
        entries = _entry_pairs(
            await self._client.xrange(stream, min=entry_id, max=entry_id, count=1)
        )
        for found_id, fields in entries:
            if found_id == entry_id:
                return fields
        return None

    async def acquire_lease(self, meeting_id: str, owner: str) -> bool:
        result = await self._client.set(_lease_key(meeting_id), owner, nx=True, px=self._lease_ms)
        return bool(result)

    async def renew_lease(self, meeting_id: str, owner: str) -> bool:
        result = await cast(
            "Awaitable[str]",
            self._client.eval(_LEASE_RENEW, 1, _lease_key(meeting_id), owner, str(self._lease_ms)),
        )
        return bool(result)

    async def release_lease(self, meeting_id: str, owner: str) -> None:
        await cast(
            "Awaitable[str]",
            self._client.eval(_LEASE_RELEASE, 1, _lease_key(meeting_id), owner),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def candidate_meetings(self) -> list[str]:
        found: set[str] = set()
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(
                cursor=cursor, match=_SCAN_MATCH, count=_SCAN_COUNT
            )
            for key in keys:
                name = str(key)
                if name.startswith("fakeyou:meeting:") and name.endswith(":commands"):
                    found.add(name[len("fakeyou:meeting:") : -len(":commands")])
            if cursor == 0:
                break
        return sorted(found)

    @classmethod
    def from_settings(cls, settings: Settings) -> "StreamTransport":
        redis_settings = settings.transport.redis
        url = require_secret(redis_settings.url, ConfigKey.TRANSPORT_REDIS_URL)
        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=redis_settings.connect_timeout_seconds,
        )
        return cls(
            client,
            stream_max_len=redis_settings.stream_max_len,
            lease_seconds=redis_settings.lease_seconds,
        )
