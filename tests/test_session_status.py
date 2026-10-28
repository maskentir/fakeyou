from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine

from fakeyou.db.session_status import SessionStatusStore

NOW = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)


def test_a_new_session_starts_created_and_cannot_be_created_twice(
    migrated_engine: Engine,
) -> None:
    store = SessionStatusStore(migrated_engine)

    created = store.create("m1", "owner-1", NOW)

    assert created is not None
    assert (created.desired_state, created.phase, created.terminal_reason) == (
        "created",
        "created",
        None,
    )
    assert store.create("m1", "owner-1", LATER) is None
    assert store.get("m1") == created


def test_the_owner_lists_only_their_sessions_in_creation_order(migrated_engine: Engine) -> None:
    store = SessionStatusStore(migrated_engine)
    store.create("m2", "owner-1", LATER)
    store.create("m1", "owner-1", NOW)
    store.create("m3", "owner-2", NOW)

    assert [status.meeting_id for status in store.list_for("owner-1")] == ["m1", "m2"]


def test_a_terminal_phase_keeps_its_reason_and_detail_and_is_final(
    migrated_engine: Engine,
) -> None:
    store = SessionStatusStore(migrated_engine)
    store.create("m1", "owner-1", NOW)
    store.record_phase("m1", "active", NOW)

    ended = store.record_phase(
        "m1", "ended", LATER, terminal_reason="left_by_participant", detail="asked to leave"
    )
    late = store.record_phase("m1", "active", LATER + timedelta(seconds=1))

    assert ended is not None
    assert ended.terminal
    assert (ended.terminal_reason, ended.detail) == ("left_by_participant", "asked to leave")
    assert late is None
    assert store.get("m1") == ended


def test_desired_state_is_written_without_touching_the_phase(migrated_engine: Engine) -> None:
    store = SessionStatusStore(migrated_engine)
    store.create("m1", "owner-1", NOW)
    store.record_phase("m1", "lobby", NOW)

    wanted = store.desire("m1", "joined", LATER)

    assert wanted is not None
    assert (wanted.desired_state, wanted.phase) == ("joined", "lobby")
    assert store.desire("missing", "stopped", LATER) is None


def test_desire_changes_only_a_different_live_wish(migrated_engine: Engine) -> None:
    store = SessionStatusStore(migrated_engine)
    store.create("m1", "owner-1", NOW)

    first = store.desire("m1", "joined", LATER)
    second = store.desire("m1", "joined", LATER)
    store.record_phase("m1", "ended", LATER, terminal_reason="ended_by_host")

    assert first is not None
    assert second is None
    assert store.desire("m1", "stopped", LATER) is None


def test_a_discarded_session_is_gone(migrated_engine: Engine) -> None:
    store = SessionStatusStore(migrated_engine)
    store.create("m1", "owner-1", NOW)

    store.discard("m1")

    assert store.get("m1") is None
    assert store.create("m1", "owner-1", LATER) is not None
