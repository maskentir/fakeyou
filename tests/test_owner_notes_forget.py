import pytest

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.owner_notes import (
    NoteIdentity,
    forget_owner_note,
    read_note_states,
    remember_owner_note,
)
from tests._owner_notes_support import (
    BODY,
    GROUP,
    NOTE_A,
    NOTE_B,
    OLDER_AT,
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
