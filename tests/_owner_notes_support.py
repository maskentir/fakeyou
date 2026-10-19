import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, TypedDict, Unpack, cast
from uuid import UUID

from graphiti_core import Graphiti
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType
from neo4j.exceptions import ConstraintError

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.memory.owner_notes import OWNER_NOTE_SOURCE

GROUP = "test_notes_alpha"


OTHER_GROUP = "test_notes_beta"


SOURCE_AT = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)


OLDER_AT = datetime(2026, 9, 30, 3, 10, tzinfo=UTC)


BODY = "Orchid demo moves to Friday."


NOTE_A = UUID("11111111-1111-4111-8111-111111111111")


NOTE_B = UUID("22222222-2222-4222-8222-222222222222")


NOTE_C = UUID("33333333-3333-4333-8333-333333333333")


@dataclass(eq=False, kw_only=True)
class FakeEpisode:
    uuid: str
    name: str
    group_id: str
    labels: list[str]
    source: EpisodeType
    content: str
    source_description: str
    created_at: datetime
    valid_at: datetime
    entity_edges: list[str] = field(default_factory=list)

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


@dataclass(eq=False, kw_only=True)
class FakeEdge:
    uuid: str
    fact: str
    episodes: list[str]
    group_id: str
    invalid_at: datetime | None = None
    expired_at: datetime | None = None

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
        if query == owner_notes_module.INTENT_CREATE_QUERY:
            return await self._create_intent(**kwargs)
        if query == owner_notes_module.CONTROL_LOAD_QUERY:
            matches = [
                control
                for key, control in store.controls.items()
                if key == kwargs["uuid"] and control["group_id"] == kwargs["group_id"]
            ]
            return SimpleNamespace(records=matches)
        if query == owner_notes_module.STATES_QUERY:
            if "states" in store.fail_next:
                store.fail_next.discard("states")
                raise RuntimeError("injected states failure")
            rows = [
                {"uuid": key, "status": store.controls[key]["status"]}
                for key in kwargs["uuids"]
                if key in store.controls and store.controls[key]["group_id"] == kwargs["group_id"]
            ]
            return SimpleNamespace(records=rows)
        if query == owner_notes_module.INDEX_QUERY:
            store.indexes += 1
            return SimpleNamespace(records=[])
        if query == owner_notes_module.FORGET_CHAIN_QUERY:
            return SimpleNamespace(records=[self._chain_counts(kwargs["uuid"], kwargs["group_id"])])
        if query == owner_notes_module.CONTROL_DELETE_QUERY:
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
        if query == owner_notes_module.LOCK_TARGET_QUERY:
            return FakeResult(self._lock_rows(kwargs["target_uuid"], kwargs["group_id"]))
        if query == owner_notes_module.PUBLISH_NOTE_QUERY:
            return FakeResult(self._publish_note(**kwargs))
        if query == owner_notes_module.PUBLISH_CORRECTION_QUERY:
            return FakeResult(self._publish_correction(**kwargs))
        if query == owner_notes_module.PUBLISH_UNTRACKED_CORRECTION_QUERY:
            return FakeResult(self._publish_untracked_correction(**kwargs))
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

    def _publish_untracked_correction(self, **kwargs: Any) -> list[dict[str, Any]]:
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


class AddEpisodeFields(TypedDict):
    name: str
    episode_body: str
    source_description: str
    reference_time: datetime
    source: EpisodeType
    group_id: str
    uuid: str


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

    async def add_episode(self, **fields: Unpack[AddEpisodeFields]) -> Any:
        name = fields["name"]
        episode_body = fields["episode_body"]
        source_description = fields["source_description"]
        reference_time = fields["reference_time"]
        source = fields["source"]
        group_id = fields["group_id"]
        uuid = fields["uuid"]
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


async def seed_untracked_note(
    store: NoteStore,
    uuid: UUID,
    body: str,
    source_at: datetime,
    label: str = OWNER_NOTE_SOURCE,
) -> Any:
    episode = FakeEpisode(
        uuid=str(uuid),
        name=f"owner-note:{uuid}",
        group_id=GROUP,
        labels=[],
        source=EpisodeType.text,
        content=body,
        source_description=label,
        created_at=source_at,
        valid_at=source_at,
    )
    await episode.save(store.driver)
    return episode
