from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from fakeyou.config.settings import StorageSettings
from fakeyou.persistence.segments import (
    S3ObjectStore,
    SegmentFile,
    SegmentSync,
    restore_segments,
)
from tests.conftest import ObjectStorageEndpoint


def _storage(endpoint: ObjectStorageEndpoint) -> StorageSettings:
    return StorageSettings(
        bucket=endpoint.bucket,
        endpoint_url=endpoint.endpoint_url,
        region="us-east-1",
        access_key_id=endpoint.access_key_id,
        secret_access_key=SecretStr(endpoint.secret_access_key),
    )


@pytest.mark.container
async def test_the_real_object_store_roundtrips_segments_byte_identical(
    object_storage_endpoint: ObjectStorageEndpoint, tmp_path: Path
) -> None:
    store = S3ObjectStore(_storage(object_storage_endpoint))
    try:
        assert await store.ensure_bucket() is True
        assert await store.ensure_bucket() is False

        record = tmp_path / "record.jsonl"
        record.write_bytes(b'{"a":1}\n{"b":2}\n')
        sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])

        assert await sync.push() == 16
        with record.open("ab") as handle:
            handle.write(b'{"c":3}\n')
        assert await sync.push() == 8

        restored = tmp_path / "restored.jsonl"
        written = await restore_segments(store, "m-1", "record", restored)

        assert written == len(record.read_bytes())
        assert restored.read_bytes() == record.read_bytes()
    finally:
        await store.aclose()


@pytest.mark.container
async def test_a_missing_bucket_is_reported_not_silently_empty(
    object_storage_endpoint: ObjectStorageEndpoint, tmp_path: Path
) -> None:
    settings = _storage(object_storage_endpoint)
    settings = settings.model_copy(update={"bucket": f"{object_storage_endpoint.bucket}-absent"})
    store = S3ObjectStore(settings)
    try:
        record = tmp_path / "record.jsonl"
        record.write_bytes(b"one\n")
        sync = SegmentSync(store, "m-absent", [SegmentFile("record", record)])

        with pytest.raises(ClientError):
            await sync.push()
    finally:
        await store.aclose()
