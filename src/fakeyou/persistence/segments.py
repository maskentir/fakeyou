from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import aioboto3
from botocore.exceptions import ClientError

from fakeyou.config.settings import Settings, StorageSettings
from fakeyou.persistence.files import write_atomically

SEGMENT_PREFIX = "sessions"


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def list(self, prefix: str) -> list[str]: ...

    async def aclose(self) -> None: ...


class S3ObjectStore:
    def __init__(self, settings: StorageSettings) -> None:
        if not settings.bucket:
            raise ValueError("storage bucket is not configured")
        self._bucket = settings.bucket
        secret = settings.secret_access_key
        self._session = aioboto3.Session(
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=secret.get_secret_value() if secret is not None else None,
            region_name=settings.region,
        )
        self._endpoint_url = str(settings.endpoint_url) if settings.endpoint_url else None
        self._context: AbstractAsyncContextManager[Any] | None = None
        self._client: Any = None

    async def _connected(self) -> Any:
        if self._client is None:
            self._context = self._session.client("s3", endpoint_url=self._endpoint_url)
            self._client = await self._context.__aenter__()
        return self._client

    async def put(self, key: str, data: bytes) -> None:
        client = await self._connected()
        await client.put_object(Bucket=self._bucket, Key=key, Body=data)

    async def get(self, key: str) -> bytes | None:
        client = await self._connected()
        try:
            response = await client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        async with response["Body"] as body:
            data: bytes = await body.read()
            return data

    async def list(self, prefix: str) -> list[str]:
        client = await self._connected()
        keys: list[str] = []
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                keys.append(item["Key"])
        return keys

    async def ensure_bucket(self) -> bool:
        client = await self._connected()
        try:
            await client.head_bucket(Bucket=self._bucket)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"):
                raise
        else:
            return False
        try:
            await client.create_bucket(Bucket=self._bucket)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
                raise
        return True

    async def aclose(self) -> None:
        if self._context is not None and self._client is not None:
            await self._context.__aexit__(None, None, None)
        self._context = None
        self._client = None


def create_object_store(settings: Settings) -> ObjectStore | None:
    if not settings.storage.configured:
        return None
    return S3ObjectStore(settings.storage)


async def ensure_bucket(settings: Settings) -> bool:
    store = create_object_store(settings)
    if store is None:
        raise ValueError("storage is not configured")
    if not isinstance(store, S3ObjectStore):
        raise TypeError("only the S3 object store can ensure its bucket")
    try:
        return await store.ensure_bucket()
    finally:
        await store.aclose()


@dataclass(frozen=True, slots=True)
class SegmentFile:
    name: str
    path: Path


def _segment_prefix(meeting_id: str, name: str) -> str:
    return f"{SEGMENT_PREFIX}/{meeting_id}/{name}/"


def _segment_key(meeting_id: str, name: str, start: int, end: int) -> str:
    return f"{_segment_prefix(meeting_id, name)}{start:012d}-{end:012d}"


def _segment_bounds(key: str) -> tuple[int, int] | None:
    start_text, _, end_text = key.rsplit("/", 1)[-1].partition("-")
    if not start_text.isdigit() or not end_text.isdigit():
        return None
    return int(start_text), int(end_text)


def _read_from(path: Path, offset: int) -> bytes:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read()
    except FileNotFoundError:
        return b""


def _local_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


@dataclass(slots=True)
class SegmentSync:
    store: ObjectStore
    meeting_id: str
    files: Sequence[SegmentFile]
    _offsets: dict[str, int] = field(default_factory=dict, init=False)

    def prime(self, restored: dict[str, int]) -> None:
        for segment in self.files:
            self._offsets[segment.name] = restored.get(segment.name, 0)

    async def push(self) -> int:
        uploaded = 0
        for segment in self.files:
            offset = self._offsets.get(segment.name, 0)
            data = await asyncio.to_thread(_read_from, segment.path, offset)
            cut = data.rfind(b"\n") + 1
            if cut == 0:
                continue
            chunk = data[:cut]
            await self.store.put(
                _segment_key(self.meeting_id, segment.name, offset, offset + cut), chunk
            )
            self._offsets[segment.name] = offset + cut
            uploaded += cut
        return uploaded


async def restore_segments(store: ObjectStore, meeting_id: str, name: str, path: Path) -> int:
    keys = sorted(await store.list(_segment_prefix(meeting_id, name)))
    assembled = bytearray()
    for key in keys:
        bounds = _segment_bounds(key)
        if bounds is None:
            continue
        start, end = bounds
        if start > len(assembled):
            break
        if end <= len(assembled):
            continue
        chunk = await store.get(key)
        if chunk is None or len(chunk) != end - start:
            break
        assembled.extend(chunk[len(assembled) - start :])
    if len(assembled) <= await asyncio.to_thread(_local_size, path):
        return 0
    await asyncio.to_thread(write_atomically, path, bytes(assembled))
    return len(assembled)
