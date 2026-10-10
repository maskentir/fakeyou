from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Column, Connection, Engine, Table, func, inspect, select
from sqlalchemy.types import TypeEngine

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.tables import include_object, metadata, voiceprints

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_LOCK_KEY = 735812049172
COMPARE_OPTIONS: dict[str, Any] = {
    "compare_type": True,
    "compare_server_default": True,
    "include_object": include_object,
}


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    current: str | None
    head: str
    differences: tuple[str, ...]


def head_revision() -> str:
    heads = ScriptDirectory.from_config(_script_config()).get_heads()
    if len(heads) != 1:
        raise RuntimeError(f"expected exactly one migration head, found {len(heads)}")
    return heads[0]


def status(engine: Engine) -> SchemaStatus:
    with engine.connect() as connection:
        return SchemaStatus(
            current=_current_revision(connection),
            head=head_revision(),
            differences=_differences(connection),
        )


def require_current(engine: Engine) -> None:
    state = status(engine)
    if state.current != state.head:
        if state.current is None:
            raise FakeYouError(ErrorCode.DATABASE_SCHEMA_EMPTY, expected=state.head)
        raise FakeYouError(
            ErrorCode.DATABASE_SCHEMA_OUTDATED, found=state.current, expected=state.head
        )
    if state.differences:
        raise FakeYouError(
            ErrorCode.DATABASE_SCHEMA_DRIFT, differences="; ".join(state.differences)
        )


def upgrade(engine: Engine) -> None:
    with engine.begin() as connection:
        _lock(connection)
        if _current_revision(connection) is None and inspect(connection).has_table(
            voiceprints.name
        ):
            raise FakeYouError(ErrorCode.DATABASE_SCHEMA_UNVERSIONED)
        command.upgrade(_migration_config(connection), "head")


def stamp(engine: Engine) -> None:
    with engine.begin() as connection:
        _lock(connection)
        differences = _differences(connection)
        if differences:
            raise FakeYouError(ErrorCode.DATABASE_STAMP_REFUSED, differences="; ".join(differences))
        command.stamp(_migration_config(connection), head_revision())


def _script_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def _migration_config(connection: Connection) -> Config:
    config = _script_config()
    config.attributes["connection"] = connection
    return config


def _lock(connection: Connection) -> None:
    connection.execute(select(func.pg_advisory_xact_lock(SCHEMA_LOCK_KEY)))


def _current_revision(connection: Connection) -> str | None:
    return MigrationContext.configure(connection).get_current_revision()


def _differences(connection: Connection) -> tuple[str, ...]:
    context = MigrationContext.configure(connection, opts=COMPARE_OPTIONS)
    return tuple(_describe(item) for item in compare_metadata(context, metadata))


def _describe(difference: object) -> str:
    if isinstance(difference, list):
        return "; ".join(_describe(item) for item in cast("list[object]", difference))
    if isinstance(difference, tuple):
        words = [_word(part) for part in cast("tuple[object, ...]", difference)]
        return " ".join(word for word in words if word)
    return str(difference)


def _word(part: object) -> str:
    if isinstance(part, str):
        return part
    if isinstance(part, Table | Column):
        return part.name
    if isinstance(part, TypeEngine):
        return str(part)
    return ""
