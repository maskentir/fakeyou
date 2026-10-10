import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from graphiti_core import Graphiti
from graphiti_core.driver.query_executor import Transaction
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EpisodeType, EpisodicNode
from neo4j.exceptions import ConstraintError
from neo4j.time import DateTime as Neo4jDateTime

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.invariants import IDENTIFIER_PATTERN

OWNER_NOTE_SOURCE = "owner note"

NoteStatus = Literal["pending", "active", "superseded"]

_GROUP_PATTERN = re.compile(IDENTIFIER_PATTERN)

_INTENT_CREATE_QUERY = (
    "CREATE (n:FakeyouOwnerNote {uuid: $uuid, group_id: $group_id, digest: $digest, "
    "status: 'pending', source_at: $source_at, recorded_at: $recorded_at, "
    "target_uuid: $target_uuid}) RETURN n.uuid AS uuid"
)
_CONTROL_LOAD_QUERY = (
    "MATCH (n:FakeyouOwnerNote {uuid: $uuid}) WHERE n.group_id = $group_id "
    "RETURN n.uuid AS uuid, n.group_id AS group_id, n.digest AS digest, "
    "n.status AS status, n.source_at AS source_at, n.recorded_at AS recorded_at, "
    "n.target_uuid AS target_uuid, n.superseded_by AS superseded_by"
)
_STATES_QUERY = (
    "MATCH (n:FakeyouOwnerNote) WHERE n.uuid IN $uuids AND n.group_id = $group_id "
    "RETURN n.uuid AS uuid, n.status AS status"
)
_INDEX_QUERY = "CREATE CONSTRAINT IF NOT EXISTS FOR (n:FakeyouOwnerNote) REQUIRE n.uuid IS UNIQUE"
_LOCK_TARGET_QUERY = (
    "MATCH (t:FakeyouOwnerNote {uuid: $target_uuid}) WHERE t.group_id = $group_id "
    "SET t.uuid = t.uuid RETURN t.status AS status"
)
_PUBLISH_NOTE_QUERY = (
    "MATCH (n:FakeyouOwnerNote {uuid: $uuid}) "
    "WHERE n.group_id = $group_id AND n.status = 'pending' "
    "MATCH (e:Episodic {uuid: $uuid}) WHERE e.group_id = $group_id "
    "SET n.status = 'active' MERGE (n)-[:SOURCE_EPISODE]->(e) RETURN n.uuid AS uuid"
)
_PUBLISH_CORRECTION_QUERY = (
    "MATCH (n:FakeyouOwnerNote {uuid: $uuid}) "
    "WHERE n.group_id = $group_id AND n.status = 'pending' "
    "MATCH (t:FakeyouOwnerNote {uuid: $target_uuid}) "
    "WHERE t.group_id = $group_id AND t.status = 'active' "
    "MATCH (e:Episodic {uuid: $uuid}) WHERE e.group_id = $group_id "
    "MATCH (te:Episodic {uuid: $target_uuid}) WHERE te.group_id = $group_id "
    "SET n.status = 'active', t.status = 'superseded', t.superseded_by = $uuid "
    "MERGE (n)-[:SOURCE_EPISODE]->(e) MERGE (n)-[:SUPERSEDES]->(t) "
    "MERGE (t)-[:SOURCE_EPISODE]->(te) RETURN n.uuid AS uuid"
)
_PUBLISH_LEGACY_CORRECTION_QUERY = (
    "MATCH (n:FakeyouOwnerNote {uuid: $uuid}) "
    "WHERE n.group_id = $group_id AND n.status = 'pending' "
    "MATCH (e:Episodic {uuid: $uuid}) WHERE e.group_id = $group_id "
    "MATCH (te:Episodic {uuid: $target_uuid}) WHERE te.group_id = $group_id "
    "CREATE (t:FakeyouOwnerNote {uuid: $target_uuid, group_id: $group_id, "
    "digest: $target_digest, status: 'superseded', source_at: $target_source_at, "
    "recorded_at: $target_recorded_at, superseded_by: $uuid}) "
    "SET n.status = 'active' "
    "MERGE (n)-[:SOURCE_EPISODE]->(e) MERGE (n)-[:SUPERSEDES]->(t) "
    "MERGE (t)-[:SOURCE_EPISODE]->(te) RETURN n.uuid AS uuid"
)
_FORGET_CHAIN_QUERY = (
    "MATCH (e:Episodic {uuid: $uuid}) WHERE e.group_id = $group_id "
    "OPTIONAL MATCH (:FakeyouOwnerNote {uuid: $uuid, group_id: $group_id})"
    "<-[:SUPERSEDES]-(incoming:FakeyouOwnerNote {group_id: $group_id}) "
    "OPTIONAL MATCH "
    "(pending:FakeyouOwnerNote {group_id: $group_id, target_uuid: $uuid, status: 'pending'}) "
    "RETURN count(DISTINCT incoming) AS incoming, "
    "count(DISTINCT pending) AS pending_correctors"
)
_CONTROL_DELETE_QUERY = (
    "MATCH (n:FakeyouOwnerNote {uuid: $uuid}) WHERE n.group_id = $group_id DETACH DELETE n"
)


@dataclass(frozen=True, slots=True)
class OwnerNoteResult:
    episode_uuid: str
    facts: tuple[str, ...]
    source_at: datetime
    recorded_at: datetime
    supersedes: str | None


class NoteConflictError(FakeYouError):
    pass


@dataclass(frozen=True, slots=True)
class _StoredNote:
    uuid: str
    group_id: str
    digest: str
    status: NoteStatus
    source_at: datetime
    recorded_at: datetime
    target_uuid: str | None
    superseded_by: str | None


@dataclass(frozen=True, slots=True)
class _Operation:
    uuid: str
    body: str
    source_at: datetime
    recorded_at: datetime
    target: str | None
    target_source_at: datetime | None
    target_recorded_at: datetime | None
    target_digest: str | None


def _require_group(group_id: str) -> None:
    if not _GROUP_PATTERN.fullmatch(group_id):
        raise FakeYouError(ErrorCode.NOTE_GROUP_ID_INVALID)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FakeYouError(ErrorCode.NOTE_TIME_NOT_AWARE, name=name)


def _digest(group_id: str, body: str, source_at: datetime, target: str | None) -> str:
    canonical = "\n".join([group_id, body, source_at.astimezone(UTC).isoformat(), target or ""])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _record_text(record: Any, key: str) -> str | None:
    value = record[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise FakeYouError(ErrorCode.NOTE_STORED_FIELD_INVALID, key=key)
    return value


def _record_moment(record: Any, key: str) -> datetime:
    value = record[key]
    if isinstance(value, Neo4jDateTime):
        value = value.to_native()
    if not isinstance(value, datetime):
        raise FakeYouError(ErrorCode.NOTE_STORED_FIELD_INVALID, key=key)
    if value.tzinfo is None or value.utcoffset() is None:
        raise FakeYouError(ErrorCode.NOTE_STORED_FIELD_INVALID, key=key)
    return value


def _record_status(record: Any) -> NoteStatus:
    value = record["status"]
    if value == "pending":
        return "pending"
    if value == "active":
        return "active"
    if value == "superseded":
        return "superseded"
    raise FakeYouError(ErrorCode.NOTE_STORED_STATUS_INVALID)


def _record_count(record: Any, key: str) -> int:
    value = record[key]
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise FakeYouError(ErrorCode.NOTE_STORED_FIELD_INVALID, key=key)


def _stored_from_record(record: Any) -> _StoredNote:
    uuid_value = _record_text(record, "uuid")
    group_value = _record_text(record, "group_id")
    digest_value = _record_text(record, "digest")
    if not uuid_value or not group_value or not digest_value:
        raise FakeYouError(ErrorCode.NOTE_STORED_IDENTITY_INVALID)
    return _StoredNote(
        uuid=uuid_value,
        group_id=group_value,
        digest=digest_value,
        status=_record_status(record),
        source_at=_record_moment(record, "source_at"),
        recorded_at=_record_moment(record, "recorded_at"),
        target_uuid=_record_text(record, "target_uuid"),
        superseded_by=_record_text(record, "superseded_by"),
    )


async def _load_control(memory: Graphiti, group_id: str, uuid: str) -> _StoredNote | None:
    result = await memory.driver.execute_query(_CONTROL_LOAD_QUERY, uuid=uuid, group_id=group_id)
    for record in result.records:
        return _stored_from_record(record)
    return None


async def _require_control(memory: Graphiti, group_id: str, uuid: str) -> _StoredNote:
    stored = await _load_control(memory, group_id, uuid)
    if stored is None:
        raise FakeYouError(ErrorCode.NOTE_RECORD_MISSING)
    return stored


async def _run_rows(tx: Transaction, query: str, **params: Any) -> list[dict[str, Any]]:
    result = await tx.run(query, **params)
    return [dict(record) async for record in result]


async def _require_correction_target(
    memory: Graphiti, *, group_id: str, target: str, effective: datetime
) -> tuple[datetime, datetime, str]:
    try:
        episode = await EpisodicNode.get_by_uuid(memory.driver, target)
    except NodeNotFoundError:
        raise FakeYouError(ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN) from None
    if episode.group_id != group_id:
        raise FakeYouError(ErrorCode.NOTE_CORRECTION_TARGET_UNKNOWN)
    if episode.source_description != OWNER_NOTE_SOURCE:
        raise FakeYouError(ErrorCode.NOTE_CORRECTION_TARGET_NOT_OWNER_NOTE)
    control = await _load_control(memory, group_id, target)
    if control is not None and control.status != "active":
        raise NoteConflictError(ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE)
    _require_aware(episode.valid_at, "target source time")
    _require_aware(episode.created_at, "target recorded time")
    if effective <= episode.valid_at:
        raise FakeYouError(ErrorCode.NOTE_CORRECTION_NOT_NEWER)
    return (
        episode.valid_at,
        episode.created_at,
        _digest(group_id, episode.content.strip(), episode.valid_at, None),
    )


async def _extract(
    memory: Graphiti, *, group_id: str, operation: _Operation, name: str
) -> list[Any]:
    extracted = await memory.add_episode(
        name=name,
        episode_body=operation.body,
        uuid=operation.uuid,
        group_id=group_id,
        source=EpisodeType.text,
        source_description=OWNER_NOTE_SOURCE,
        reference_time=operation.source_at,
    )
    return list(extracted.edges)


def _usable_edges(edges: list[Any], *, group_id: str, uuid: str) -> list[Any]:
    return [
        edge
        for edge in edges
        if uuid in edge.episodes
        and edge.invalid_at is None
        and edge.expired_at is None
        and edge.group_id == group_id
    ]


async def _publish_note(memory: Graphiti, *, group_id: str, operation: _Operation) -> None:
    try:
        async with memory.driver.transaction() as tx:
            if operation.target is None:
                rows = await _run_rows(
                    tx, _PUBLISH_NOTE_QUERY, uuid=operation.uuid, group_id=group_id
                )
                if not rows:
                    raise NoteConflictError(ErrorCode.NOTE_NOT_PENDING)
                return
            locked = await _run_rows(
                tx, _LOCK_TARGET_QUERY, target_uuid=operation.target, group_id=group_id
            )
            if locked:
                if locked[0]["status"] != "active":
                    raise NoteConflictError(ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE)
                rows = await _run_rows(
                    tx,
                    _PUBLISH_CORRECTION_QUERY,
                    uuid=operation.uuid,
                    group_id=group_id,
                    target_uuid=operation.target,
                )
                if not rows:
                    raise NoteConflictError(ErrorCode.NOTE_CORRECTION_TARGET_INACTIVE)
                return
            if (
                operation.target_source_at is None
                or operation.target_recorded_at is None
                or operation.target_digest is None
            ):
                raise FakeYouError(ErrorCode.NOTE_TARGET_SNAPSHOT_MISSING)
            rows = await _run_rows(
                tx,
                _PUBLISH_LEGACY_CORRECTION_QUERY,
                uuid=operation.uuid,
                group_id=group_id,
                target_uuid=operation.target,
                target_source_at=operation.target_source_at,
                target_recorded_at=operation.target_recorded_at,
                target_digest=operation.target_digest,
            )
            if not rows:
                raise NoteConflictError(ErrorCode.NOTE_NOT_PENDING)
    except NoteConflictError:
        raise
    except ConstraintError as error:
        raise NoteConflictError(ErrorCode.NOTE_CORRECTION_CONCURRENT) from error


async def _result_for_active(
    memory: Graphiti, *, group_id: str, stored: _StoredNote
) -> OwnerNoteResult:
    try:
        episode = await EpisodicNode.get_by_uuid(memory.driver, stored.uuid)
    except NodeNotFoundError:
        raise FakeYouError(ErrorCode.NOTE_EPISODE_MISSING) from None
    if episode.group_id != group_id:
        raise FakeYouError(ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY)
    edges: list[Any] = []
    if episode.entity_edges:
        edges = list(await EntityEdge.get_by_uuids(memory.driver, list(episode.entity_edges)))
    usable = _usable_edges(edges, group_id=group_id, uuid=stored.uuid)
    if not usable:
        raise FakeYouError(ErrorCode.NOTE_NO_READABLE_FACTS)
    return OwnerNoteResult(
        episode_uuid=stored.uuid,
        facts=tuple(edge.fact for edge in usable),
        source_at=stored.source_at,
        recorded_at=stored.recorded_at,
        supersedes=stored.target_uuid,
    )


async def _advance_note(
    memory: Graphiti, *, group_id: str, operation: _Operation
) -> OwnerNoteResult:
    try:
        episode = await EpisodicNode.get_by_uuid(memory.driver, operation.uuid)
    except NodeNotFoundError:
        episode = None
    if episode is None:
        fresh = EpisodicNode(
            uuid=operation.uuid,
            name=f"owner-note:{operation.uuid}",
            group_id=group_id,
            labels=[],
            source=EpisodeType.text,
            content=operation.body,
            source_description=OWNER_NOTE_SOURCE,
            created_at=operation.recorded_at,
            valid_at=operation.source_at,
        )
        await fresh.save(memory.driver)
        edges = await _extract(memory, group_id=group_id, operation=operation, name=fresh.name)
    elif episode.entity_edges:
        edges = list(await EntityEdge.get_by_uuids(memory.driver, list(episode.entity_edges)))
    else:
        edges = await _extract(memory, group_id=group_id, operation=operation, name=episode.name)
    if not _usable_edges(edges, group_id=group_id, uuid=operation.uuid):
        raise FakeYouError(ErrorCode.NOTE_NO_USABLE_FACTS)
    await _publish_note(memory, group_id=group_id, operation=operation)
    stored = await _require_control(memory, group_id, operation.uuid)
    return await _result_for_active(memory, group_id=group_id, stored=stored)


async def _resume_note(
    memory: Graphiti,
    *,
    group_id: str,
    body: str,
    source_at: datetime | None,
    target: str | None,
    stored: _StoredNote,
) -> OwnerNoteResult:
    if stored.group_id != group_id:
        raise NoteConflictError(ErrorCode.NOTE_ID_IN_USE)
    if stored.status == "superseded":
        raise NoteConflictError(ErrorCode.NOTE_ALREADY_SUPERSEDED)
    if source_at is not None and source_at != stored.source_at:
        raise NoteConflictError(ErrorCode.NOTE_PAYLOAD_MISMATCH)
    if target != stored.target_uuid:
        raise NoteConflictError(ErrorCode.NOTE_PAYLOAD_MISMATCH)
    if _digest(group_id, body, stored.source_at, target) != stored.digest:
        raise NoteConflictError(ErrorCode.NOTE_PAYLOAD_MISMATCH)
    if stored.status == "active":
        return await _result_for_active(memory, group_id=group_id, stored=stored)
    target_source_at: datetime | None = None
    target_recorded_at: datetime | None = None
    target_digest: str | None = None
    if target is not None:
        target_source_at, target_recorded_at, target_digest = await _require_correction_target(
            memory, group_id=group_id, target=target, effective=stored.source_at
        )
    return await _advance_note(
        memory,
        group_id=group_id,
        operation=_Operation(
            uuid=stored.uuid,
            body=body,
            source_at=stored.source_at,
            recorded_at=stored.recorded_at,
            target=target,
            target_source_at=target_source_at,
            target_recorded_at=target_recorded_at,
            target_digest=target_digest,
        ),
    )


async def remember_owner_note(
    memory: Graphiti,
    *,
    group_id: str,
    text: str,
    source_at: datetime | None = None,
    note_id: UUID | None = None,
    supersedes: UUID | None = None,
) -> OwnerNoteResult:
    _require_group(group_id)
    body = text.strip()
    if not body:
        raise FakeYouError(ErrorCode.NOTE_BODY_BLANK)
    if source_at is not None:
        _require_aware(source_at, "source_at")
    now = datetime.now(UTC)
    if source_at is not None and source_at > now:
        raise FakeYouError(ErrorCode.NOTE_SOURCE_AT_FUTURE)
    target = str(supersedes) if supersedes is not None else None
    operation = str(note_id) if note_id is not None else str(uuid4())
    stored = await _load_control(memory, group_id, operation)
    if stored is not None:
        return await _resume_note(
            memory,
            group_id=group_id,
            body=body,
            source_at=source_at,
            target=target,
            stored=stored,
        )
    effective = now if source_at is None else source_at
    target_source_at: datetime | None = None
    target_recorded_at: datetime | None = None
    target_digest: str | None = None
    if target is not None:
        target_source_at, target_recorded_at, target_digest = await _require_correction_target(
            memory, group_id=group_id, target=target, effective=effective
        )
    try:
        await EpisodicNode.get_by_uuid(memory.driver, operation)
    except NodeNotFoundError:
        pass
    else:
        raise NoteConflictError(ErrorCode.NOTE_ID_IN_USE) from None
    try:
        await memory.driver.execute_query(
            _INTENT_CREATE_QUERY,
            uuid=operation,
            group_id=group_id,
            digest=_digest(group_id, body, effective, target),
            source_at=effective,
            recorded_at=now,
            target_uuid=target,
        )
    except ConstraintError as error:
        raise NoteConflictError(ErrorCode.NOTE_ID_IN_USE) from error
    return await _advance_note(
        memory,
        group_id=group_id,
        operation=_Operation(
            uuid=operation,
            body=body,
            source_at=effective,
            recorded_at=now,
            target=target,
            target_source_at=target_source_at,
            target_recorded_at=target_recorded_at,
            target_digest=target_digest,
        ),
    )


async def read_note_states(
    memory: Graphiti, *, group_id: str, episode_uuids: list[str]
) -> dict[str, NoteStatus]:
    _require_group(group_id)
    wanted = sorted(set(episode_uuids))
    if not wanted:
        return {}
    result = await memory.driver.execute_query(_STATES_QUERY, uuids=wanted, group_id=group_id)
    states: dict[str, NoteStatus] = {}
    for record in result.records:
        uuid_value = _record_text(record, "uuid")
        if uuid_value is None:
            raise FakeYouError(ErrorCode.NOTE_STORED_IDENTITY_INVALID)
        states[uuid_value] = _record_status(record)
    return states


async def ensure_note_indexes(memory: Graphiti) -> None:
    await memory.driver.execute_query(_INDEX_QUERY)


async def forget_owner_note(memory: Graphiti, *, group_id: str, episode_uuid: str) -> None:
    _require_group(group_id)
    if not episode_uuid.strip():
        raise FakeYouError(ErrorCode.NOTE_EPISODE_UUID_BLANK)
    try:
        episode = await EpisodicNode.get_by_uuid(memory.driver, episode_uuid)
    except NodeNotFoundError:
        raise FakeYouError(ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY) from None
    if episode.group_id != group_id:
        raise FakeYouError(ErrorCode.NOTE_EPISODE_NOT_IN_MEMORY)
    control = await _load_control(memory, group_id, episode_uuid)
    result = await memory.driver.execute_query(
        _FORGET_CHAIN_QUERY, uuid=episode_uuid, group_id=group_id
    )
    incoming = 0
    pending_correctors = 0
    for record in result.records:
        incoming = _record_count(record, "incoming")
        pending_correctors = _record_count(record, "pending_correctors")
    if pending_correctors:
        raise FakeYouError(ErrorCode.NOTE_HAS_PENDING_CORRECTION)
    if (
        control is not None
        and control.status != "pending"
        and (
            control.target_uuid is not None
            or control.superseded_by is not None
            or control.status == "superseded"
            or incoming > 0
        )
    ):
        raise FakeYouError(ErrorCode.NOTE_IN_CORRECTION_CHAIN)
    await memory.remove_episode(episode_uuid)
    await memory.driver.execute_query(_CONTROL_DELETE_QUERY, uuid=episode_uuid, group_id=group_id)
