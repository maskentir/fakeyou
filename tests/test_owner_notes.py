import asyncio
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from graphiti_core import Graphiti
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType
from neo4j.exceptions import ConstraintError

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.owner_notes import (
    OWNER_NOTE_SOURCE,
    NoteConflictError,
    NoteIdentity,
    OwnerNoteResult,
    ensure_note_indexes,
    forget_owner_note,
    read_note_states,
    remember_owner_note,
)

GROUP = "test_notes_alpha"
OTHER_GROUP = "test_notes_beta"
SOURCE_AT = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
OLDER_AT = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)
BODY = "Orchid demo moves to Friday."
NOTE_A = UUID("11111111-1111-4111-8111-111111111111")
NOTE_B = UUID("22222222-2222-4222-8222-222222222222")
NOTE_C = UUID("33333333-3333-4333-8333-333333333333")


class FakeEpisode:
    def __init__(
        self,
        *,
        uuid: str,
        name: str,
        group_id: str,
        labels: list[str],
        source: EpisodeType,
        content: str,
        source_description: str,
        created_at: datetime,
        valid_at: datetime,
    ) -> None:
        self.uuid = uuid
        self.name = name
        self.group_id = group_id
        self.labels = labels
        self.source = source
        self.content = content
        self.source_description = source_description
        self.created_at = created_at
        self.valid_at = valid_at
        self.entity_edges: list[str] = []

    async def save(self, driver: Any) -> None:
        store = driver.store
        if "precreate" in store.fail_next:
            store.fail_next.discard("precreate")
            raise RuntimeError("injected precreate failure")
        store.episodes[self.uuid] = self

    @classmethod
    async def get_by_uuid(cls, driver: Any, uuid: str) -> Any:
        try:
            return driver.store.episodes[uuid]
        except KeyError:
            raise NodeNotFoundError(uuid) from None

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[Any]:
        return [driver.store.episodes[item] for item in uuids if item in driver.store.episodes]


class FakeEdge:
    def __init__(
        self,
        *,
        uuid: str,
        fact: str,
        episodes: list[str],
        group_id: str,
        invalid_at: datetime | None = None,
        expired_at: datetime | None = None,
    ) -> None:
        self.uuid = uuid
        self.fact = fact
        self.episodes = episodes
        self.group_id = group_id
        self.invalid_at = invalid_at
        self.expired_at = expired_at

    @classmethod
    async def get_by_uuids(cls, driver: Any, uuids: list[str]) -> list[Any]:
        store = driver.store
        if "result" in store.fail_next:
            store.fail_next.discard("result")
            raise RuntimeError("injected result failure")
        return [store.edges[item] for item in uuids if item in store.edges]


class FakeResult:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def __aiter__(self) -> Any:
        for record in self._records:
            yield record


class FakeDriver:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def execute_query(self, query: str, **kwargs: Any) -> Any:
        store = self.store
        store.queries.append((query, dict(kwargs)))
        if query == owner_notes_module._INTENT_CREATE_QUERY:
            return await self._create_intent(**kwargs)
        if query == owner_notes_module._CONTROL_LOAD_QUERY:
            matches = [
                control
                for key, control in store.controls.items()
                if key == kwargs["uuid"] and control["group_id"] == kwargs["group_id"]
            ]
            return SimpleNamespace(records=matches)
        if query == owner_notes_module._STATES_QUERY:
            if "states" in store.fail_next:
                store.fail_next.discard("states")
                raise RuntimeError("injected states failure")
            rows = [
                {"uuid": key, "status": store.controls[key]["status"]}
                for key in kwargs["uuids"]
                if key in store.controls and store.controls[key]["group_id"] == kwargs["group_id"]
            ]
            return SimpleNamespace(records=rows)
        if query == owner_notes_module._INDEX_QUERY:
            store.indexes += 1
            return SimpleNamespace(records=[])
        if query == owner_notes_module._FORGET_CHAIN_QUERY:
            return SimpleNamespace(records=[self._chain_counts(kwargs["uuid"], kwargs["group_id"])])
        if query == owner_notes_module._CONTROL_DELETE_QUERY:
            await self._delete_control(kwargs["uuid"], kwargs["group_id"])
            return SimpleNamespace(records=[])
        raise AssertionError(f"unexpected query: {query}")

    async def _create_intent(self, **kwargs: Any) -> Any:
        store = self.store
        async with store.lock_for(kwargs["uuid"]):
            if kwargs["uuid"] in store.controls:
                raise ConstraintError(f"duplicate note {kwargs['uuid']}")
            store.controls[kwargs["uuid"]] = {
                "uuid": kwargs["uuid"],
                "group_id": kwargs["group_id"],
                "digest": kwargs["digest"],
                "status": "pending",
                "source_at": kwargs["source_at"],
                "recorded_at": kwargs["recorded_at"],
                "target_uuid": kwargs["target_uuid"],
                "superseded_by": None,
            }
        return SimpleNamespace(records=[{"uuid": kwargs["uuid"]}])

    def _chain_counts(self, uuid: str, group_id: str) -> dict[str, int]:
        store = self.store
        incoming = {
            rel[1]
            for rel in store.rels
            if rel[0] == "control"
            and rel[2] == "SUPERSEDES"
            and rel[3] == "control"
            and rel[4] == uuid
            and store.controls.get(rel[1], {}).get("group_id") == group_id
        }
        pending = {
            key
            for key, control in store.controls.items()
            if control["group_id"] == group_id
            and control["target_uuid"] == uuid
            and control["status"] == "pending"
        }
        return {"incoming": len(incoming), "pending_correctors": len(pending)}

    async def _delete_control(self, uuid: str, group_id: str) -> None:
        store = self.store
        async with store.lock_for(uuid):
            current = store.controls.get(uuid)
            if current is None or current["group_id"] != group_id:
                return
            del store.controls[uuid]
            store.rels = [
                rel
                for rel in store.rels
                if not (
                    (rel[0] == "control" and rel[1] == uuid)
                    or (rel[3] == "control" and rel[4] == uuid)
                )
            ]

    def transaction(self) -> Any:
        return FakeTransaction(self.store)


class FakeTransaction:
    def __init__(self, store: Any) -> None:
        self._store = store
        self._runs = 0
        self._staged: list[Any] = []
        self._held: list[Any] = []

    async def __aenter__(self) -> Any:
        if "publish" in self._store.fail_next:
            self._store.fail_next.discard("publish")
            raise RuntimeError("injected publish failure")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            if exc_type is None:
                for apply in self._staged:
                    apply()
        finally:
            for lock in self._held:
                lock.release()
            self._held = []
            self._staged = []
        return False

    async def run(self, query: str, **kwargs: Any) -> Any:
        store = self._store
        store.queries.append((query, dict(kwargs)))
        self._runs += 1
        if "late_publish" in store.fail_next and self._runs >= 2:
            store.fail_next.discard("late_publish")
            raise RuntimeError("injected late publish failure")
        if kwargs.get("target_uuid") is not None:
            await self._hold(kwargs["target_uuid"])
        if "uuid" in kwargs:
            await self._hold(kwargs["uuid"])
        await asyncio.sleep(0)
        if query == owner_notes_module._LOCK_TARGET_QUERY:
            return FakeResult(self._lock_rows(kwargs["target_uuid"], kwargs["group_id"]))
        if query == owner_notes_module._PUBLISH_NOTE_QUERY:
            return FakeResult(self._publish_note(**kwargs))
        if query == owner_notes_module._PUBLISH_CORRECTION_QUERY:
            return FakeResult(self._publish_correction(**kwargs))
        if query == owner_notes_module._PUBLISH_LEGACY_CORRECTION_QUERY:
            return FakeResult(self._publish_legacy(**kwargs))
        raise AssertionError(f"unexpected tx query: {query}")

    async def _hold(self, uuid: str) -> None:
        lock = self._store.lock_for(uuid)
        if lock not in self._held:
            await lock.acquire()
            self._held.append(lock)

    def _lock_rows(self, target_uuid: str, group_id: str) -> list[dict[str, Any]]:
        control = self._store.controls.get(target_uuid)
        if control is None or control["group_id"] != group_id:
            return []
        return [{"status": control["status"]}]

    def _publish_note(self, **kwargs: Any) -> list[dict[str, Any]]:
        store = self._store
        own = store.controls.get(kwargs["uuid"])
        episode = store.episodes.get(kwargs["uuid"])
        if (
            own is None
            or own["group_id"] != kwargs["group_id"]
            or own["status"] != "pending"
            or episode is None
            or episode.group_id != kwargs["group_id"]
        ):
            return []

        def apply() -> None:
            own["status"] = "active"
            store.link("control", kwargs["uuid"], "SOURCE_EPISODE", "episode", kwargs["uuid"])

        self._staged.append(apply)
        return [{"uuid": kwargs["uuid"]}]

    def _publish_correction(self, **kwargs: Any) -> list[dict[str, Any]]:
        store = self._store
        own = store.controls.get(kwargs["uuid"])
        target = store.controls.get(kwargs["target_uuid"])
        episode = store.episodes.get(kwargs["uuid"])
        target_episode = store.episodes.get(kwargs["target_uuid"])
        if (
            own is None
            or own["group_id"] != kwargs["group_id"]
            or own["status"] != "pending"
            or target is None
            or target["group_id"] != kwargs["group_id"]
            or target["status"] != "active"
            or episode is None
            or episode.group_id != kwargs["group_id"]
            or target_episode is None
            or target_episode.group_id != kwargs["group_id"]
        ):
            return []

        def apply() -> None:
            own["status"] = "active"
            target["status"] = "superseded"
            target["superseded_by"] = kwargs["uuid"]
            store.link("control", kwargs["uuid"], "SOURCE_EPISODE", "episode", kwargs["uuid"])
            store.link("control", kwargs["uuid"], "SUPERSEDES", "control", kwargs["target_uuid"])
            store.link(
                "control",
                kwargs["target_uuid"],
                "SOURCE_EPISODE",
                "episode",
                kwargs["target_uuid"],
            )

        self._staged.append(apply)
        return [{"uuid": kwargs["uuid"]}]

    def _publish_legacy(self, **kwargs: Any) -> list[dict[str, Any]]:
        store = self._store
        own = store.controls.get(kwargs["uuid"])
        episode = store.episodes.get(kwargs["uuid"])
        target_episode = store.episodes.get(kwargs["target_uuid"])
        if (
            own is None
            or own["group_id"] != kwargs["group_id"]
            or own["status"] != "pending"
            or episode is None
            or episode.group_id != kwargs["group_id"]
            or target_episode is None
            or target_episode.group_id != kwargs["group_id"]
        ):
            return []
        if kwargs["target_uuid"] in store.controls:
            raise ConstraintError(f"duplicate note {kwargs['target_uuid']}")

        def apply() -> None:
            store.controls[kwargs["target_uuid"]] = {
                "uuid": kwargs["target_uuid"],
                "group_id": kwargs["group_id"],
                "digest": kwargs["target_digest"],
                "status": "superseded",
                "source_at": kwargs["target_source_at"],
                "recorded_at": kwargs["target_recorded_at"],
                "target_uuid": None,
                "superseded_by": kwargs["uuid"],
            }
            own["status"] = "active"
            store.link("control", kwargs["uuid"], "SOURCE_EPISODE", "episode", kwargs["uuid"])
            store.link("control", kwargs["uuid"], "SUPERSEDES", "control", kwargs["target_uuid"])
            store.link(
                "control",
                kwargs["target_uuid"],
                "SOURCE_EPISODE",
                "episode",
                kwargs["target_uuid"],
            )

        self._staged.append(apply)
        return [{"uuid": kwargs["uuid"]}]


class NoteStore:
    def __init__(self) -> None:
        self.episodes: dict[str, Any] = {}
        self.edges: dict[str, Any] = {}
        self.controls: dict[str, dict[str, Any]] = {}
        self.rels: list[tuple[str, str, str, str, str]] = []
        self.extraction_calls = 0
        self.added: list[dict[str, Any]] = []
        self.removed: list[str] = []
        self.fail_next: set[str] = set()
        self.extract_mode = "linked"
        self.locks: dict[str, Any] = {}
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.indexes = 0
        self.driver = FakeDriver(self)
        self.memory = cast(
            Graphiti,
            SimpleNamespace(
                driver=self.driver,
                add_episode=self.add_episode,
                remove_episode=self.remove_episode,
            ),
        )

    def lock_for(self, uuid: str) -> Any:
        lock = self.locks.get(uuid)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[uuid] = lock
        return lock

    def link(self, from_kind: str, from_uuid: str, rel: str, to_kind: str, to_uuid: str) -> None:
        entry = (from_kind, from_uuid, rel, to_kind, to_uuid)
        if entry not in self.rels:
            self.rels.append(entry)

    async def add_episode(
        self,
        *,
        name: str,
        episode_body: str,
        source_description: str,
        reference_time: datetime,
        source: EpisodeType,
        group_id: str,
        uuid: str,
    ) -> Any:
        if "extract" in self.fail_next:
            self.fail_next.discard("extract")
            raise RuntimeError("injected extract failure")
        episode = self.episodes.get(uuid)
        if episode is None:
            raise NodeNotFoundError(uuid)
        self.extraction_calls += 1
        self.added.append(
            {
                "name": name,
                "episode_body": episode_body,
                "source_description": source_description,
                "reference_time": reference_time,
                "source": source,
                "group_id": group_id,
                "uuid": uuid,
            }
        )
        edges: list[Any] = []
        if self.extract_mode == "linked":
            edges = [
                FakeEdge(
                    uuid=f"{uuid}-edge-{self.extraction_calls}",
                    fact=episode_body.strip(),
                    episodes=[uuid],
                    group_id=group_id,
                )
            ]
        elif self.extract_mode == "unlinked":
            edges = [
                FakeEdge(
                    uuid=f"{uuid}-edge-{self.extraction_calls}",
                    fact=episode_body.strip(),
                    episodes=[],
                    group_id=group_id,
                )
            ]
        elif self.extract_mode == "invalid":
            edges = [
                FakeEdge(
                    uuid=f"{uuid}-edge-{self.extraction_calls}",
                    fact=episode_body.strip(),
                    episodes=[uuid],
                    group_id=group_id,
                    invalid_at=datetime.now(UTC),
                )
            ]
        elif self.extract_mode != "empty":
            raise AssertionError(f"unexpected extract mode: {self.extract_mode}")
        for edge in edges:
            self.edges[edge.uuid] = edge
        episode.entity_edges = [edge.uuid for edge in edges]
        return SimpleNamespace(episode=episode, edges=edges)

    async def remove_episode(self, episode_uuid: str) -> None:
        self.removed.append(episode_uuid)
        self.episodes.pop(episode_uuid, None)


@pytest.fixture
def note_store(monkeypatch: pytest.MonkeyPatch) -> NoteStore:
    store = NoteStore()
    monkeypatch.setattr(owner_notes_module, "EpisodicNode", FakeEpisode)
    monkeypatch.setattr(owner_notes_module, "EntityEdge", FakeEdge)
    return store


async def _seed_legacy(
    store: NoteStore,
    uuid: UUID,
    group_id: str,
    body: str,
    source_at: datetime,
    label: str = OWNER_NOTE_SOURCE,
) -> Any:
    episode = FakeEpisode(
        uuid=str(uuid),
        name=f"owner-note:{uuid}",
        group_id=group_id,
        labels=[],
        source=EpisodeType.text,
        content=body,
        source_description=label,
        created_at=source_at,
        valid_at=source_at,
    )
    await episode.save(store.driver)
    return episode


async def test_remember_new_note_keeps_source_time(note_store: NoteStore) -> None:
    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert isinstance(result, OwnerNoteResult)
    assert result.episode_uuid == str(NOTE_A)
    assert result.facts == (BODY,)
    assert result.source_at == SOURCE_AT
    assert result.recorded_at.tzinfo is not None
    assert result.recorded_at > SOURCE_AT
    assert result.supersedes is None
    episode = note_store.episodes[str(NOTE_A)]
    assert episode.valid_at == SOURCE_AT
    assert episode.created_at == result.recorded_at
    assert episode.content == BODY
    assert episode.source_description == OWNER_NOTE_SOURCE
    assert episode.group_id == GROUP
    assert episode.source == EpisodeType.text
    control = note_store.controls[str(NOTE_A)]
    assert control["status"] == "active"
    assert control["digest"]
    assert control["target_uuid"] is None
    assert ("control", str(NOTE_A), "SOURCE_EPISODE", "episode", str(NOTE_A)) in note_store.rels
    assert note_store.extraction_calls == 1
    [added] = note_store.added
    assert added["uuid"] == str(NOTE_A)
    assert added["episode_body"] == BODY
    assert added["reference_time"] == SOURCE_AT
    assert added["source_description"] == OWNER_NOTE_SOURCE
    assert added["group_id"] == GROUP
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "active"}


async def test_new_note_without_times_uses_now(note_store: NoteStore) -> None:
    before = datetime.now(UTC)

    result = await remember_owner_note(note_store.memory, group_id=GROUP, text=BODY)

    after = datetime.now(UTC)
    assert before <= result.source_at <= after
    assert result.source_at == result.recorded_at
    UUID(result.episode_uuid)
    assert note_store.episodes[result.episode_uuid].valid_at == result.source_at


async def test_blank_text_is_rejected_before_extraction(note_store: NoteStore) -> None:
    for text in ("", "   "):
        with pytest.raises(FakeYouError, match="must not be blank") as caught:
            await remember_owner_note(note_store.memory, group_id=GROUP, text=text)
        assert caught.value.code is ErrorCode.NOTE_BODY_BLANK

    assert note_store.extraction_calls == 0
    assert note_store.controls == {}
    assert note_store.episodes == {}


@pytest.mark.parametrize("group_id", ["", "bad:group", "bad group", "x" * 129])
async def test_invalid_group_is_rejected(note_store: NoteStore, group_id: str) -> None:
    with pytest.raises(FakeYouError, match="group_id") as caught:
        await remember_owner_note(note_store.memory, group_id=group_id, text=BODY)

    assert caught.value.code is ErrorCode.NOTE_GROUP_ID_INVALID
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_naive_source_time_is_rejected(note_store: NoteStore) -> None:
    with pytest.raises(FakeYouError, match="timezone") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=datetime(2026, 9, 30, 6, 40),
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_TIME_NOT_AWARE
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_future_source_time_is_rejected(note_store: NoteStore) -> None:
    future = datetime.now(UTC) + timedelta(days=1)

    with pytest.raises(FakeYouError, match="future") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=future,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_SOURCE_AT_FUTURE
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_note_id_taken_by_existing_episode_is_rejected(
    note_store: NoteStore,
) -> None:
    await _seed_legacy(note_store, NOTE_A, GROUP, BODY, OLDER_AT)

    with pytest.raises(NoteConflictError, match="already in use") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_ID_IN_USE
    assert note_store.extraction_calls == 0
    assert str(NOTE_A) not in note_store.controls


async def test_retry_published_note_does_not_extract_twice(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    calls = note_store.extraction_calls

    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == first
    assert note_store.extraction_calls == calls
    assert len(note_store.episodes) == 1
    assert len(note_store.controls) == 1
    assert len([rel for rel in note_store.rels if rel[2] == "SOURCE_EPISODE"]) == 1
    assert note_store.removed == []


async def test_retry_without_source_time_uses_stored_time(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    again = await remember_owner_note(
        note_store.memory, group_id=GROUP, text=BODY, identity=NoteIdentity(NOTE_A)
    )

    assert again == first
    assert again.source_at == SOURCE_AT
    assert again.recorded_at == first.recorded_at
    assert note_store.extraction_calls == 1


async def test_retry_with_equivalent_timezone_offset_matches(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    shifted = SOURCE_AT.astimezone(timezone(timedelta(hours=7)))
    assert shifted == SOURCE_AT
    assert shifted.utcoffset() != SOURCE_AT.utcoffset()

    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=shifted,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == first
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_text_is_rejected(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text="Orchid demo moves to Monday.",
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_source_time_is_rejected(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=OLDER_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_target_is_rejected(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 2


async def test_correction_publishes_and_supersedes_target(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    assert result.episode_uuid == str(NOTE_B)
    assert result.facts == (BODY,)
    assert result.source_at == SOURCE_AT
    assert result.supersedes == str(NOTE_A)
    states = await read_note_states(
        note_store.memory,
        group_id=GROUP,
        episode_uuids=[str(NOTE_A), str(NOTE_B)],
    )
    assert states == {str(NOTE_A): "superseded", str(NOTE_B): "active"}
    target_control = note_store.controls[str(NOTE_A)]
    assert target_control["superseded_by"] == str(NOTE_B)
    target_episode = note_store.episodes[str(NOTE_A)]
    assert target_episode.valid_at == OLDER_AT
    assert target_episode.content == "Orchid demo moves to Thursday."
    assert ("control", str(NOTE_B), "SUPERSEDES", "control", str(NOTE_A)) in note_store.rels
    assert note_store.removed == []
    assert note_store.extraction_calls == 2


async def test_correction_of_legacy_note_creates_superseded_record(
    note_store: NoteStore,
) -> None:
    await _seed_legacy(note_store, NOTE_A, GROUP, "Orchid demo moves to Thursday.", OLDER_AT)

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    assert result.supersedes == str(NOTE_A)
    states = await read_note_states(
        note_store.memory,
        group_id=GROUP,
        episode_uuids=[str(NOTE_A), str(NOTE_B)],
    )
    assert states == {str(NOTE_A): "superseded", str(NOTE_B): "active"}
    target_control = note_store.controls[str(NOTE_A)]
    assert target_control["source_at"] == OLDER_AT
    assert target_control["superseded_by"] == str(NOTE_B)
    assert ("control", str(NOTE_B), "SUPERSEDES", "control", str(NOTE_A)) in note_store.rels
    assert note_store.removed == []


async def test_correction_rejects_unknown_target(note_store: NoteStore) -> None:
    with pytest.raises(FakeYouError, match="unknown correction target") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, uuid4()),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_correction_target_in_other_group_is_hidden(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=OTHER_GROUP,
        text=BODY,
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(FakeYouError, match="unknown correction target") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN
    assert note_store.extraction_calls == 1
    assert str(NOTE_B) not in note_store.controls


async def test_correction_rejects_transcript_target(note_store: NoteStore) -> None:
    await _seed_legacy(
        note_store,
        NOTE_A,
        GROUP,
        "Orchid demo moves to Thursday.",
        OLDER_AT,
        label="Google Meet m1 transcript",
    )

    with pytest.raises(FakeYouError, match="must be an owner note") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE
    assert note_store.extraction_calls == 0
    assert str(NOTE_B) not in note_store.controls


async def test_correction_rejects_replaced_target(note_store: NoteStore) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="no longer active") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text="Orchid demo moves to Monday.",
            source_at=datetime(2026, 10, 1, 6, 40, tzinfo=UTC),
            identity=NoteIdentity(NOTE_C, NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE
    assert note_store.extraction_calls == 2
    assert str(NOTE_C) not in note_store.controls


async def test_correction_rejects_pending_target(note_store: NoteStore) -> None:
    note_store.fail_next.add("publish")
    with pytest.raises(RuntimeError, match="injected publish failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text="Orchid demo moves to Thursday.",
            source_at=OLDER_AT,
            identity=NoteIdentity(NOTE_A),
        )

    with pytest.raises(NoteConflictError, match="no longer active") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE
    assert str(NOTE_B) not in note_store.controls


@pytest.mark.parametrize(
    "source_at",
    [OLDER_AT, datetime(2026, 9, 30, 2, 10, tzinfo=UTC)],
)
async def test_correction_requires_newer_source_time(
    note_store: NoteStore, source_at: datetime
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(FakeYouError, match="newer than its target") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=source_at,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_CORRECTION_NOT_NEWER
    assert note_store.extraction_calls == 1
    assert str(NOTE_B) not in note_store.controls


async def test_empty_extraction_never_publishes(note_store: NoteStore) -> None:
    note_store.extract_mode = "empty"

    with pytest.raises(FakeYouError, match="no usable facts") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_NO_USABLE_FACTS
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "pending"}
    note_store.extract_mode = "linked"

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert result.facts == (BODY,)
    assert note_store.extraction_calls == 2
    assert len(note_store.episodes) == 1


@pytest.mark.parametrize("mode", ["unlinked", "invalid"])
async def test_extraction_without_usable_evidence_never_publishes(
    note_store: NoteStore, mode: str
) -> None:
    note_store.extract_mode = mode

    with pytest.raises(FakeYouError, match="no usable facts") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_NO_USABLE_FACTS
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "pending"}


async def test_fault_after_prepare_resumes(note_store: NoteStore) -> None:
    note_store.fail_next.add("precreate")

    with pytest.raises(RuntimeError, match="injected precreate failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert note_store.episodes == {}
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "pending"}

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert result.facts == (BODY,)
    assert note_store.extraction_calls == 1
    assert len(note_store.episodes) == 1
    assert len(note_store.controls) == 1


async def test_fault_after_episode_saved_resumes_with_single_extraction(
    note_store: NoteStore,
) -> None:
    note_store.fail_next.add("extract")

    with pytest.raises(RuntimeError, match="injected extract failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert len(note_store.episodes) == 1
    assert note_store.episodes[str(NOTE_A)].entity_edges == []

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert result.facts == (BODY,)
    assert note_store.extraction_calls == 1
    assert len(note_store.controls) == 1


async def test_fault_after_extraction_resumes_without_reextracting(
    note_store: NoteStore,
) -> None:
    note_store.fail_next.add("publish")

    with pytest.raises(RuntimeError, match="injected publish failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert note_store.extraction_calls == 1
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "pending"}

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert result.facts == (BODY,)
    assert note_store.extraction_calls == 1
    assert len(note_store.rels) == 1


async def test_fault_before_commit_rolls_back_and_resumes(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    note_store.fail_next.add("late_publish")

    with pytest.raises(RuntimeError, match="injected late publish failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    states = await read_note_states(
        note_store.memory,
        group_id=GROUP,
        episode_uuids=[str(NOTE_A), str(NOTE_B)],
    )
    assert states == {str(NOTE_A): "active", str(NOTE_B): "pending"}
    assert note_store.rels == [("control", str(NOTE_A), "SOURCE_EPISODE", "episode", str(NOTE_A))]

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    assert result.supersedes == str(NOTE_A)
    assert note_store.extraction_calls == 2
    assert len([rel for rel in note_store.rels if rel[2] == "SUPERSEDES"]) == 1
    states = await read_note_states(
        note_store.memory,
        group_id=GROUP,
        episode_uuids=[str(NOTE_A), str(NOTE_B)],
    )
    assert states == {str(NOTE_A): "superseded", str(NOTE_B): "active"}


async def test_fault_after_commit_returns_active_on_retry(
    note_store: NoteStore,
) -> None:
    note_store.fail_next.add("result")

    with pytest.raises(RuntimeError, match="injected result failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "active"}

    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == result
    assert result.facts == (BODY,)
    assert note_store.extraction_calls == 1
    assert len(note_store.episodes) == 1
    assert len(note_store.controls) == 1
    assert len(note_store.rels) == 1


async def test_concurrent_corrections_keep_single_winner(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    bodies = {str(NOTE_B): BODY, str(NOTE_C): "Orchid demo moves to Monday."}

    async def correct(note_id: UUID) -> OwnerNoteResult:
        return await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=bodies[str(note_id)],
            source_at=SOURCE_AT,
            identity=NoteIdentity(note_id, NOTE_A),
        )

    first, second = await asyncio.gather(correct(NOTE_B), correct(NOTE_C), return_exceptions=True)

    outcomes = sorted(type(item).__name__ for item in (first, second))
    assert outcomes == ["NoteConflictError", "OwnerNoteResult"]
    winner = first if isinstance(first, OwnerNoteResult) else second
    assert isinstance(winner, OwnerNoteResult)
    loser_id = str(NOTE_C) if winner.episode_uuid == str(NOTE_B) else str(NOTE_B)
    states = await read_note_states(
        note_store.memory,
        group_id=GROUP,
        episode_uuids=[str(NOTE_A), str(NOTE_B), str(NOTE_C)],
    )
    assert states[str(NOTE_A)] == "superseded"
    assert states[winner.episode_uuid] == "active"
    assert states[loser_id] == "pending"
    assert len([rel for rel in note_store.rels if rel[2] == "SUPERSEDES"]) == 1

    repeat = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=bodies[winner.episode_uuid],
        source_at=SOURCE_AT,
        identity=NoteIdentity(UUID(winner.episode_uuid), NOTE_A),
    )

    assert repeat == winner


async def test_forget_blocks_correction_chain(note_store: NoteStore) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B, NOTE_A),
    )

    with pytest.raises(FakeYouError, match="correction chain") as caught:
        await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_A))
    assert caught.value.code is ErrorCode.NOTE_IN_CORRECTION_CHAIN
    with pytest.raises(FakeYouError, match="correction chain") as caught:
        await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_B))
    assert caught.value.code is ErrorCode.NOTE_IN_CORRECTION_CHAIN

    assert note_store.removed == []
    assert len(note_store.episodes) == 2
    assert len(note_store.controls) == 2


async def test_forget_blocks_target_with_pending_correction(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text="Orchid demo moves to Thursday.",
        source_at=OLDER_AT,
        identity=NoteIdentity(NOTE_A),
    )
    note_store.fail_next.add("publish")
    with pytest.raises(RuntimeError, match="injected publish failure"):
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B, NOTE_A),
        )

    with pytest.raises(FakeYouError, match="pending correction") as caught:
        await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_A))
    assert caught.value.code is ErrorCode.NOTE_HAS_PENDING_CORRECTION

    await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_B))
    await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_A))

    assert note_store.removed == [str(NOTE_B), str(NOTE_A)]
    assert note_store.episodes == {}
    assert note_store.controls == {}


async def test_forget_removes_standalone_note_with_control(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    await remember_owner_note(
        note_store.memory,
        group_id=OTHER_GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_B),
    )

    await forget_owner_note(note_store.memory, group_id=GROUP, episode_uuid=str(NOTE_A))

    assert note_store.removed == [str(NOTE_A)]
    assert str(NOTE_A) not in note_store.episodes
    assert str(NOTE_A) not in note_store.controls
    assert str(NOTE_B) in note_store.episodes
    assert str(NOTE_B) in note_store.controls


async def test_forget_rejects_foreign_group(note_store: NoteStore) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(FakeYouError, match="not in this persona") as caught:
        await forget_owner_note(note_store.memory, group_id=OTHER_GROUP, episode_uuid=str(NOTE_A))

    assert caught.value.code is ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY
    assert note_store.removed == []
    assert str(NOTE_A) in note_store.episodes


async def test_control_record_survives_episode_resave(note_store: NoteStore) -> None:
    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    await note_store.episodes[result.episode_uuid].save(note_store.driver)

    states = await read_note_states(
        note_store.memory, group_id=GROUP, episode_uuids=[result.episode_uuid]
    )
    assert states == {result.episode_uuid: "active"}
    assert note_store.controls[result.episode_uuid]["digest"]


async def test_states_hide_other_groups_and_unknown_ids(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert (
        await read_note_states(note_store.memory, group_id=OTHER_GROUP, episode_uuids=[str(NOTE_A)])
        == {}
    )
    assert (
        await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=["missing"]) == {}
    )
    assert await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[]) == {}


async def test_states_failure_is_not_reported_as_active(
    note_store: NoteStore,
) -> None:
    note_store.fail_next.add("states")

    with pytest.raises(RuntimeError, match="injected states failure"):
        await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])


async def test_retry_reads_neo4j_native_datetimes(note_store: NoteStore) -> None:
    from neo4j.time import DateTime as Neo4jDateTime

    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    control = note_store.controls[str(NOTE_A)]
    control["source_at"] = Neo4jDateTime.from_native(first.source_at)
    control["recorded_at"] = Neo4jDateTime.from_native(first.recorded_at)

    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == first
    assert note_store.extraction_calls == 1


async def test_ensure_note_indexes_requests_unique_uuid(
    note_store: NoteStore,
) -> None:
    await ensure_note_indexes(note_store.memory)
    await ensure_note_indexes(note_store.memory)

    assert note_store.indexes == 2
    assert note_store.queries[0][0] == owner_notes_module._INDEX_QUERY
    assert "REQUIRE n.uuid IS UNIQUE" in note_store.queries[0][0]
