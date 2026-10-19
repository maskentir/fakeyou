import pytest

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.memory.owner_notes import NoteIdentity, read_note_states, remember_owner_note
from tests._owner_notes_support import (
    BODY,
    GROUP,
    NOTE_A,
    OTHER_GROUP,
    SOURCE_AT,
    FakeEdge,
    FakeEpisode,
    NoteStore,
)


@pytest.fixture
def note_store(monkeypatch: pytest.MonkeyPatch) -> NoteStore:
    store = NoteStore()
    monkeypatch.setattr(owner_notes_module, "EpisodicNode", FakeEpisode)
    monkeypatch.setattr(owner_notes_module, "EntityEdge", FakeEdge)
    return store


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
