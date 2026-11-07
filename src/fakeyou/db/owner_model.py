from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, cast
from uuid import uuid4

from sqlalchemy import Select, func, insert, select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.db.engine import configured_url, create_async_engine_for_url, require_url
from fakeyou.db.schema import require_current_on
from fakeyou.db.tables import owner_model_revisions

OwnerModelKind = Literal["position", "preference", "decision_rule", "style_example", "fact"]
Confidence = Literal["firm", "leaning"]
ItemDisclosure = Literal["room_speakable", "owner_only"]
RevisionSource = Literal["owner", "import", "debrief"]
RevisionStatus = Literal["proposed", "approved", "rejected", "retired"]

OWNER_AUTHORED: frozenset[RevisionSource] = frozenset({"owner", "import"})
DECIDED: tuple[RevisionStatus, ...] = ("approved", "retired")


@dataclass(frozen=True, slots=True)
class TwinScope:
    owner_id: str
    twin_id: str


@dataclass(frozen=True, slots=True)
class OwnerModelEntry:
    kind: OwnerModelKind
    topic: str
    statement: str
    confidence: Confidence = "firm"
    disclosure: ItemDisclosure = "room_speakable"
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerModelRevision:
    revision_id: str
    item_id: str
    revision: int
    scope: TwinScope
    entry: OwnerModelEntry
    source: RevisionSource
    status: RevisionStatus
    model_version: int | None
    created_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class OwnerModelSnapshot:
    version: int
    items: tuple[OwnerModelRevision, ...]


class OwnerModelRepository(Protocol):
    async def add(
        self, scope: TwinScope, entry: OwnerModelEntry, source: RevisionSource, at: datetime
    ) -> OwnerModelRevision: ...

    async def revise(
        self,
        scope: TwinScope,
        item_id: str,
        entry: OwnerModelEntry,
        source: RevisionSource,
        at: datetime,
    ) -> OwnerModelRevision: ...

    async def retire(self, scope: TwinScope, item_id: str, at: datetime) -> OwnerModelRevision: ...

    async def decide(
        self, scope: TwinScope, revision_id: str, *, approve: bool, at: datetime
    ) -> OwnerModelRevision: ...

    async def snapshot(
        self, scope: TwinScope, version: int | None = None
    ) -> OwnerModelSnapshot: ...

    async def proposals(self, scope: TwinScope) -> list[OwnerModelRevision]: ...

    async def history(self, scope: TwinScope, item_id: str) -> list[OwnerModelRevision]: ...


def _revision(row: RowMapping) -> OwnerModelRevision:
    return OwnerModelRevision(
        revision_id=row["revision_id"],
        item_id=row["item_id"],
        revision=row["revision"],
        scope=TwinScope(row["owner_id"], row["twin_id"]),
        entry=OwnerModelEntry(
            kind=cast(OwnerModelKind, row["kind"]),
            topic=row["topic"],
            statement=row["statement"],
            confidence=cast(Confidence, row["confidence"]),
            disclosure=cast(ItemDisclosure, row["disclosure"]),
            reason=row["reason"],
        ),
        source=cast(RevisionSource, row["source"]),
        status=cast(RevisionStatus, row["status"]),
        model_version=row["model_version"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def _in_scope(scope: TwinScope) -> Select[tuple[object, ...]]:
    table = owner_model_revisions
    return select(table).where(table.c.owner_id == scope.owner_id, table.c.twin_id == scope.twin_id)


@contextmanager
def _reachable() -> Iterator[None]:
    try:
        yield
    except (OperationalError, DisconnectionError) as error:
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE) from error


class OwnerModelStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._schema_current = False

    async def close(self) -> None:
        await self._engine.dispose()

    async def require_current(self) -> None:
        if self._schema_current:
            return
        with _reachable():
            async with self._engine.connect() as connection:
                await connection.run_sync(require_current_on)
        self._schema_current = True

    async def add(
        self, scope: TwinScope, entry: OwnerModelEntry, source: RevisionSource, at: datetime
    ) -> OwnerModelRevision:
        with _reachable():
            async with self._engine.begin() as connection:
                await _lock(connection, scope)
                return await _write(connection, scope, _Row(uuid4().hex, 1, entry, source), at)

    async def revise(
        self,
        scope: TwinScope,
        item_id: str,
        entry: OwnerModelEntry,
        source: RevisionSource,
        at: datetime,
    ) -> OwnerModelRevision:
        with _reachable():
            async with self._engine.begin() as connection:
                await _lock(connection, scope)
                revisions = await _item(connection, scope, item_id)
                _require_open(revisions, item_id)
                row = _Row(item_id, revisions[-1].revision + 1, entry, source)
                return await _write(connection, scope, row, at)

    async def retire(self, scope: TwinScope, item_id: str, at: datetime) -> OwnerModelRevision:
        with _reachable():
            async with self._engine.begin() as connection:
                await _lock(connection, scope)
                revisions = await _item(connection, scope, item_id)
                _require_open(revisions, item_id)
                approved = [revision for revision in revisions if revision.status == "approved"]
                if not approved:
                    raise FakeYouError(ErrorCode.OWNER_MODEL_ITEM_UNKNOWN, item_id=item_id)
                row = _Row(item_id, revisions[-1].revision + 1, approved[-1].entry, "owner")
                decision = _Decision("retired", await _next_version(connection, scope), at)
                return await _insert(connection, scope, row, decision)

    async def decide(
        self, scope: TwinScope, revision_id: str, *, approve: bool, at: datetime
    ) -> OwnerModelRevision:
        table = owner_model_revisions
        with _reachable():
            async with self._engine.begin() as connection:
                await _lock(connection, scope)
                row = (
                    (
                        await connection.execute(
                            _in_scope(scope).where(table.c.revision_id == revision_id)
                        )
                    )
                    .mappings()
                    .first()
                )
                if row is None or row["status"] != "proposed":
                    raise FakeYouError(ErrorCode.OWNER_MODEL_NOT_PROPOSED, revision_id=revision_id)
                if approve:
                    _require_open(await _item(connection, scope, row["item_id"]), row["item_id"])
                version = await _next_version(connection, scope) if approve else None
                updated = (
                    (
                        await connection.execute(
                            update(table)
                            .where(table.c.revision_id == revision_id)
                            .values(
                                status="approved" if approve else "rejected",
                                model_version=version,
                                decided_at=at,
                            )
                            .returning(table)
                        )
                    )
                    .mappings()
                    .one()
                )
                return _revision(updated)

    async def snapshot(self, scope: TwinScope, version: int | None = None) -> OwnerModelSnapshot:
        table = owner_model_revisions
        with _reachable():
            async with self._engine.connect() as connection:
                latest_version = await _next_version(connection, scope) - 1
                bound = latest_version if version is None else min(version, latest_version)
                latest = (
                    _in_scope(scope)
                    .where(table.c.status.in_(DECIDED), table.c.model_version <= bound)
                    .order_by(table.c.item_id, table.c.model_version.desc())
                    .ext(distinct_on(table.c.item_id))
                )
                rows = (await connection.execute(latest)).mappings().all()
        items = tuple(
            sorted(
                (_revision(row) for row in rows if row["status"] == "approved"),
                key=lambda item: item.model_version or 0,
            )
        )
        return OwnerModelSnapshot(version=bound, items=items)

    async def proposals(self, scope: TwinScope) -> list[OwnerModelRevision]:
        table = owner_model_revisions
        with _reachable():
            async with self._engine.connect() as connection:
                rows = (
                    await connection.execute(
                        _in_scope(scope)
                        .where(table.c.status == "proposed")
                        .order_by(table.c.created_at)
                    )
                ).mappings()
                return [_revision(row) for row in rows]

    async def history(self, scope: TwinScope, item_id: str) -> list[OwnerModelRevision]:
        with _reachable():
            async with self._engine.connect() as connection:
                revisions = await _item(connection, scope, item_id)
        if not revisions:
            raise FakeYouError(ErrorCode.OWNER_MODEL_ITEM_UNKNOWN, item_id=item_id)
        return revisions


@dataclass(frozen=True, slots=True)
class _Decision:
    status: RevisionStatus
    version: int | None
    at: datetime | None


@dataclass(frozen=True, slots=True)
class _Row:
    item_id: str
    revision: int
    entry: OwnerModelEntry
    source: RevisionSource


async def _lock(connection: AsyncConnection, scope: TwinScope) -> None:
    key = f"owner_model:{scope.owner_id}:{scope.twin_id}"
    await connection.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


async def _next_version(connection: AsyncConnection, scope: TwinScope) -> int:
    table = owner_model_revisions
    current = (
        await connection.execute(
            select(func.max(table.c.model_version)).where(
                table.c.owner_id == scope.owner_id, table.c.twin_id == scope.twin_id
            )
        )
    ).scalar()
    return (current or 0) + 1


async def _item(
    connection: AsyncConnection, scope: TwinScope, item_id: str
) -> list[OwnerModelRevision]:
    table = owner_model_revisions
    rows = (
        await connection.execute(
            _in_scope(scope).where(table.c.item_id == item_id).order_by(table.c.revision)
        )
    ).mappings()
    return [_revision(row) for row in rows]


def _require_open(revisions: Sequence[OwnerModelRevision], item_id: str) -> None:
    if not revisions:
        raise FakeYouError(ErrorCode.OWNER_MODEL_ITEM_UNKNOWN, item_id=item_id)
    decided = [revision for revision in revisions if revision.status in DECIDED]
    if decided and decided[-1].status == "retired":
        raise FakeYouError(ErrorCode.OWNER_MODEL_ITEM_RETIRED, item_id=item_id)


async def _write(
    connection: AsyncConnection, scope: TwinScope, row: _Row, at: datetime
) -> OwnerModelRevision:
    if row.source in OWNER_AUTHORED:
        decision = _Decision("approved", await _next_version(connection, scope), at)
    else:
        decision = _Decision("proposed", None, None)
    return await _insert(connection, scope, row, decision, created_at=at)


async def _insert(
    connection: AsyncConnection,
    scope: TwinScope,
    row: _Row,
    decision: _Decision,
    *,
    created_at: datetime | None = None,
) -> OwnerModelRevision:
    table = owner_model_revisions
    entry = row.entry
    inserted = (
        (
            await connection.execute(
                insert(table)
                .values(
                    revision_id=uuid4().hex,
                    item_id=row.item_id,
                    revision=row.revision,
                    owner_id=scope.owner_id,
                    twin_id=scope.twin_id,
                    kind=entry.kind,
                    topic=entry.topic,
                    statement=entry.statement,
                    reason=entry.reason,
                    confidence=entry.confidence,
                    disclosure=entry.disclosure,
                    source=row.source,
                    status=decision.status,
                    model_version=decision.version,
                    created_at=created_at or decision.at,
                    decided_at=decision.at,
                )
                .returning(table)
            )
        )
        .mappings()
        .one()
    )
    return _revision(inserted)


def create_owner_model(settings: Settings) -> OwnerModelStore:
    database = settings.database
    url = require_url(configured_url(settings))
    return OwnerModelStore(
        create_async_engine_for_url(
            url,
            connect_timeout_seconds=database.connect_timeout_seconds,
            statement_timeout_seconds=database.statement_timeout_seconds,
        )
    )
