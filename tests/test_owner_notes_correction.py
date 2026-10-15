from datetime import UTC, datetime
from uuid import uuid4

import pytest

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.owner_notes import (
    NoteConflictError,
    NoteIdentity,
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
    OTHER_GROUP,
    SOURCE_AT,
    FakeEdge,
    FakeEpisode,
    NoteStore,
    seed_untracked_note,
)


@pytest.fixture
def note_store(monkeypatch: pytest.MonkeyPatch) -> NoteStore:
    store = NoteStore()
    monkeypatch.setattr(owner_notes_module, "EpisodicNode", FakeEpisode)
    monkeypatch.setattr(owner_notes_module, "EntityEdge", FakeEdge)
    return store


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


async def test_correction_of_untracked_note_creates_superseded_record(
    note_store: NoteStore,
) -> None:
    await seed_untracked_note(note_store, NOTE_A, "Orchid demo moves to Thursday.", OLDER_AT)

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
    await seed_untracked_note(
        note_store,
        NOTE_A,
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
