from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, func, inspect, select, text

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.db.engine import create_engine_for_url
from fakeyou.db.schema import (
    MIGRATIONS_DIR,
    SCHEMA_LOCK_KEY,
    SchemaStatus,
    head_revision,
    require_current,
    stamp,
    status,
    upgrade,
)
from fakeyou.db.tables import voiceprints

BASELINE = MIGRATIONS_DIR / "versions" / "0001_voiceprints.py"
BASELINE_COLUMNS = [
    "person_id",
    "embedding",
    "dim",
    "n_utterances",
    "consent_ts",
    "policy_version",
    "is_negative",
    "enrolled_at",
    "last_used_at",
    "owner_name",
]


@pytest.fixture
def blank_engine(blank_database_url: str) -> Iterator[Engine]:
    engine = create_engine_for_url(blank_database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def head_status() -> SchemaStatus:
    head = head_revision()
    return SchemaStatus(current=head, head=head, differences=())


def add_drift_column(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE voiceprints ADD COLUMN drift TEXT"))


def test_head_revision_is_the_revision_declared_by_the_baseline_migration() -> None:
    spec = importlib.util.spec_from_file_location("baseline_migration", BASELINE)
    assert spec is not None and spec.loader is not None
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)

    assert head_revision() == baseline.revision


def test_migration_files_live_inside_the_package() -> None:
    assert (MIGRATIONS_DIR / "env.py").is_file()
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert BASELINE.is_file()
    assert MIGRATIONS_DIR.name == "migrations"


def test_upgrade_on_a_blank_database_reaches_head_without_differences(
    blank_engine: Engine,
) -> None:
    upgrade(blank_engine)

    assert status(blank_engine) == head_status()


def test_upgrade_is_idempotent(blank_engine: Engine) -> None:
    upgrade(blank_engine)
    upgrade(blank_engine)

    assert status(blank_engine) == head_status()


def test_upgrade_creates_voiceprints_with_the_baseline_columns(migrated_engine: Engine) -> None:
    columns = [column["name"] for column in inspect(migrated_engine).get_columns("voiceprints")]

    assert columns == BASELINE_COLUMNS


def test_status_on_a_blank_database_has_no_current_revision(blank_engine: Engine) -> None:
    state = status(blank_engine)

    assert state.current is None
    assert state.head == head_revision()


def test_upgrade_refuses_a_voiceprints_table_without_migration_history(
    blank_engine: Engine,
) -> None:
    voiceprints.create(blank_engine)

    with pytest.raises(FakeYouError, match="fakeyou db stamp") as error:
        upgrade(blank_engine)

    assert error.value.code is ErrorCode.DATABASE_SCHEMA_UNVERSIONED
    assert status(blank_engine).current is None
    assert not inspect(blank_engine).has_table("alembic_version")


def test_stamp_adopts_a_matching_unversioned_table(blank_engine: Engine) -> None:
    voiceprints.create(blank_engine)

    stamp(blank_engine)
    upgrade(blank_engine)

    assert status(blank_engine) == head_status()


def test_stamp_refuses_a_table_with_drift_and_records_nothing(blank_engine: Engine) -> None:
    voiceprints.create(blank_engine)
    add_drift_column(blank_engine)

    with pytest.raises(FakeYouError, match="does not match the baseline migration") as error:
        stamp(blank_engine)

    assert error.value.code is ErrorCode.DATABASE_STAMP_REFUSED
    assert status(blank_engine).current is None


def test_require_current_refuses_a_blank_database(blank_engine: Engine) -> None:
    with pytest.raises(FakeYouError, match="no migrations applied") as error:
        require_current(blank_engine)

    assert error.value.code is ErrorCode.DATABASE_SCHEMA_EMPTY


def test_require_current_passes_on_a_migrated_database(migrated_engine: Engine) -> None:
    require_current(migrated_engine)


def test_require_current_refuses_schema_drift(migrated_engine: Engine) -> None:
    add_drift_column(migrated_engine)

    with pytest.raises(FakeYouError, match="voiceprints drifted from the migrated schema") as error:
        require_current(migrated_engine)

    assert error.value.code is ErrorCode.DATABASE_SCHEMA_DRIFT


def test_require_current_refuses_a_different_revision(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num = '0000'"))

    with pytest.raises(
        FakeYouError, match=f"schema is at 0000, expected {head_revision()}"
    ) as error:
        require_current(migrated_engine)

    assert error.value.code is ErrorCode.DATABASE_SCHEMA_OUTDATED


def test_require_current_refuses_an_empty_version_table(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as connection:
        connection.execute(text("DELETE FROM alembic_version"))

    with pytest.raises(FakeYouError, match="no migrations applied") as error:
        require_current(migrated_engine)

    assert error.value.code is ErrorCode.DATABASE_SCHEMA_EMPTY


def test_baseline_downgrade_refuses_to_drop_biometric_data(migrated_engine: Engine) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))

    with (
        pytest.raises(RuntimeError, match="refusing to drop voiceprints"),
        migrated_engine.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "base")

    assert inspect(migrated_engine).has_table("voiceprints")
    assert status(migrated_engine).current == head_revision()


def test_upgrade_waits_while_another_session_holds_the_schema_lock(
    blank_engine: Engine, blank_database_url: str
) -> None:
    holder_engine = create_engine_for_url(blank_database_url)
    try:
        with holder_engine.connect() as holder, ThreadPoolExecutor(max_workers=1) as pool:
            transaction = holder.begin()
            holder.execute(select(func.pg_advisory_xact_lock(SCHEMA_LOCK_KEY)))
            pending = pool.submit(upgrade, blank_engine)
            try:
                with pytest.raises(TimeoutError):
                    pending.result(timeout=1)
            finally:
                transaction.commit()
            pending.result(timeout=30)
    finally:
        holder_engine.dispose()

    assert status(blank_engine) == head_status()
