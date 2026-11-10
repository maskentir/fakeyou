import json
from collections.abc import Mapping
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.typing import EncodableT, FieldT

from fakeyou.catalog.config import ConfigKey
from fakeyou.config.settings import Settings, require_secret
from fakeyou.transport.schemas import event_stream, validate_event

OVERRIDE_TTL_SECONDS = 120.0
CONNECTION_ERRORS: tuple[type[Exception], ...] = (RedisConnectionError, RedisTimeoutError)


def _overrides_key(scope: str) -> str:
    return f"fakeyou:overrides:{scope}"


def _entry_pairs(entries: Any) -> list[tuple[str, dict[str, str]]]:
    return [(str(entry_id), dict(fields)) for entry_id, fields in entries]


class StreamTransport:
    def __init__(self, client: Redis, *, stream_max_len: int) -> None:
        self._client = client
        self._stream_max_len = stream_max_len

    async def _publish(self, stream: str, fields: Mapping[str, str]) -> str:
        payload: dict[FieldT, EncodableT] = {name: value for name, value in fields.items()}
        return cast(
            str,
            await self._client.xadd(stream, payload, maxlen=self._stream_max_len, approximate=True),
        )

    async def publish_event(self, meeting_id: str, fields: Mapping[str, str]) -> str:
        validated = validate_event(fields, meeting_id=meeting_id)
        return await self._publish(event_stream(meeting_id), validated)

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

    async def read_events(
        self, meeting_id: str, *, after: str, count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        entries = await self._client.xrange(
            event_stream(meeting_id), min=f"({after}", max="+", count=count
        )
        return _entry_pairs(entries)

    async def aclose(self) -> None:
        await self._client.aclose()

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
        )
