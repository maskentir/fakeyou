from pathlib import Path

from fakeyou.config.settings import Settings, StorageSettings
from fakeyou.persistence.segments import (
    SegmentFile,
    SegmentSync,
    create_object_store,
    push_appended_segments,
    restore_segments,
)
from tests._fakes_common import env_file_opt


class FakeStore:
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


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


async def test_segments_roundtrip_byte_identical(tmp_path: Path) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b'{"a":1}\n{"b":2}\n')
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])

    _append(record, b'{"c":3}\n{"d":4}\n')
    assert await sync.push() > 0
    _append(record, b'{"e":5}\n')
    assert await sync.push() > 0

    restored = tmp_path / "restored.jsonl"
    written = await restore_segments(store, "m-1", "record", restored)

    assert restored.read_bytes() == record.read_bytes()
    assert written == len(record.read_bytes())


async def test_a_partial_line_waits_for_its_newline(tmp_path: Path) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b"one\ntw")
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])

    assert await sync.push() == 4

    _append(record, b"o\nthree\n")
    assert await sync.push() == 10

    restored = tmp_path / "restored.jsonl"
    await restore_segments(store, "m-1", "record", restored)
    assert restored.read_bytes() == b"one\ntwo\nthree\n"


async def test_restore_never_shrinks_a_longer_local_file(tmp_path: Path) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b"one\n")
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])
    await sync.push()
    _append(record, b"two\nthree\n")

    assert await restore_segments(store, "m-1", "record", record) == 0
    assert record.read_bytes() == b"one\ntwo\nthree\n"


async def test_restore_stops_at_the_first_gap(tmp_path: Path) -> None:
    store = FakeStore()
    store.objects["sessions/m-1/record/000000000000-000000000004"] = b"one\n"
    store.objects["sessions/m-1/record/000000000009-000000000014"] = b"three"
    record = tmp_path / "record.jsonl"

    written = await restore_segments(store, "m-1", "record", record)

    assert written == 4
    assert record.read_bytes() == b"one\n"


async def test_priming_with_a_restored_length_uploads_only_the_tail(tmp_path: Path) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b"one\ntwo\n")
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])
    sync.prime({"record": 8})

    _append(record, b"three\n")
    assert await sync.push() == 6
    assert list(store.objects) == ["sessions/m-1/record/000000000008-000000000014"]


async def test_restore_merges_an_overlapping_segment(tmp_path: Path) -> None:
    store = FakeStore()
    store.objects["sessions/m-1/record/000000000000-000000000008"] = b"one\ntwo\n"
    store.objects["sessions/m-1/record/000000000000-000000000014"] = b"one\ntwo\nthree\n"
    record = tmp_path / "record.jsonl"

    written = await restore_segments(store, "m-1", "record", record)

    assert written == 14
    assert record.read_bytes() == b"one\ntwo\nthree\n"


async def test_pushing_appended_segments_uploads_only_the_post_session_tail(
    tmp_path: Path,
) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b"one\ntwo\n")
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])
    await sync.push()
    _append(record, b"three\nfour\n")

    pushed = await push_appended_segments(store, "m-1", [SegmentFile("record", record)])

    assert pushed == 11
    assert list(store.objects) == [
        "sessions/m-1/record/000000000000-000000000008",
        "sessions/m-1/record/000000000008-000000000019",
    ]
    restored = tmp_path / "restored.jsonl"
    assert await restore_segments(store, "m-1", "record", restored) == 19
    assert restored.read_bytes() == record.read_bytes()


async def test_pushing_appended_segments_waits_for_a_complete_line(tmp_path: Path) -> None:
    store = FakeStore()
    record = tmp_path / "record.jsonl"
    record.write_bytes(b"one\n")
    sync = SegmentSync(store, "m-1", [SegmentFile("record", record)])
    await sync.push()
    _append(record, b"tw")

    assert await push_appended_segments(store, "m-1", [SegmentFile("record", record)]) == 0
    assert list(store.objects) == ["sessions/m-1/record/000000000000-000000000004"]


def test_a_blank_bucket_means_storage_is_not_configured() -> None:
    assert StorageSettings(bucket="").configured is False
    assert StorageSettings(bucket="   ").configured is False
    assert StorageSettings(bucket=None).configured is False
    assert StorageSettings(bucket="fakeyou").configured is True


def test_no_object_store_without_a_bucket() -> None:
    settings = Settings(**env_file_opt(None))
    settings = settings.model_copy(update={"storage": StorageSettings(bucket="")})

    assert create_object_store(settings) is None
