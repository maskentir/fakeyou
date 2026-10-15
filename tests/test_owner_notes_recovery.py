import asyncio
from uuid import UUID

import pytest

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.owner_notes import (
    NoteIdentity,
    OwnerNoteResult,
    read_note_states,
    remember_owner_note,
)
from tests._owner_notes_support import (
    BODY,
    GROUP,
    NOTE_A,
    NOTE_B,
    NOTE_C,
    OLDER_AT,
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
