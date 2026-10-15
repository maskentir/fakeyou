from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from graphiti_core.nodes import EpisodeType

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.owner_notes import (
    OWNER_NOTE_SOURCE,
    NoteConflictError,
    NoteIdentity,
    OwnerNoteResult,
    ensure_note_indexes,
    read_note_states,
    remember_owner_note,
)
from tests._owner_notes_support import (
    BODY,
    GROUP,
    NOTE_A,
    NOTE_B,
    OLDER_AT,
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


async def test_remember_new_note_keeps_source_time(note_store: NoteStore) -> None:
    result = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert isinstance(result, OwnerNoteResult)
    assert result.episode_uuid == str(NOTE_A)
    assert result.facts == (BODY,)
    assert result.source_at == SOURCE_AT
    assert result.recorded_at.tzinfo is not None
    assert result.recorded_at > SOURCE_AT
    assert result.supersedes is None
    episode = note_store.episodes[str(NOTE_A)]
    assert episode.valid_at == SOURCE_AT
    assert episode.created_at == result.recorded_at
    assert episode.content == BODY
    assert episode.source_description == OWNER_NOTE_SOURCE
    assert episode.group_id == GROUP
    assert episode.source == EpisodeType.text
    control = note_store.controls[str(NOTE_A)]
    assert control["status"] == "active"
    assert control["digest"]
    assert control["target_uuid"] is None
    assert ("control", str(NOTE_A), "SOURCE_EPISODE", "episode", str(NOTE_A)) in note_store.rels
    assert note_store.extraction_calls == 1
    [added] = note_store.added
    assert added["uuid"] == str(NOTE_A)
    assert added["episode_body"] == BODY
    assert added["reference_time"] == SOURCE_AT
    assert added["source_description"] == OWNER_NOTE_SOURCE
    assert added["group_id"] == GROUP
    states = await read_note_states(note_store.memory, group_id=GROUP, episode_uuids=[str(NOTE_A)])
    assert states == {str(NOTE_A): "active"}


async def test_new_note_without_times_uses_now(note_store: NoteStore) -> None:
    before = datetime.now(UTC)

    result = await remember_owner_note(note_store.memory, group_id=GROUP, text=BODY)

    after = datetime.now(UTC)
    assert before <= result.source_at <= after
    assert result.source_at == result.recorded_at
    UUID(result.episode_uuid)
    assert note_store.episodes[result.episode_uuid].valid_at == result.source_at


async def test_blank_text_is_rejected_before_extraction(note_store: NoteStore) -> None:
    for text in ("", "   "):
        with pytest.raises(FakeYouError, match="must not be blank") as caught:
            await remember_owner_note(note_store.memory, group_id=GROUP, text=text)
        assert caught.value.code is ErrorCode.NOTE_BODY_BLANK

    assert note_store.extraction_calls == 0
    assert note_store.controls == {}
    assert note_store.episodes == {}


@pytest.mark.parametrize("group_id", ["", "bad:group", "bad group", "x" * 129])
async def test_invalid_group_is_rejected(note_store: NoteStore, group_id: str) -> None:
    with pytest.raises(FakeYouError, match="group_id") as caught:
        await remember_owner_note(note_store.memory, group_id=group_id, text=BODY)

    assert caught.value.code is ErrorCode.NOTE_GROUP_ID_INVALID
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_naive_source_time_is_rejected(note_store: NoteStore) -> None:
    with pytest.raises(FakeYouError, match="timezone") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=datetime(2026, 9, 30, 6, 40),
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_TIME_NOT_AWARE
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_future_source_time_is_rejected(note_store: NoteStore) -> None:
    future = datetime.now(UTC) + timedelta(days=1)

    with pytest.raises(FakeYouError, match="future") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=future,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_SOURCE_AT_FUTURE
    assert note_store.extraction_calls == 0
    assert note_store.controls == {}


async def test_note_id_taken_by_existing_episode_is_rejected(
    note_store: NoteStore,
) -> None:
    await seed_untracked_note(note_store, NOTE_A, BODY, OLDER_AT)

    with pytest.raises(NoteConflictError, match="already in use") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_ID_IN_USE
    assert note_store.extraction_calls == 0
    assert str(NOTE_A) not in note_store.controls


async def test_retry_published_note_does_not_extract_twice(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    calls = note_store.extraction_calls

    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == first
    assert note_store.extraction_calls == calls
    assert len(note_store.episodes) == 1
    assert len(note_store.controls) == 1
    assert len([rel for rel in note_store.rels if rel[2] == "SOURCE_EPISODE"]) == 1
    assert note_store.removed == []


async def test_retry_without_source_time_uses_stored_time(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    again = await remember_owner_note(
        note_store.memory, group_id=GROUP, text=BODY, identity=NoteIdentity(NOTE_A)
    )

    assert again == first
    assert again.source_at == SOURCE_AT
    assert again.recorded_at == first.recorded_at
    assert note_store.extraction_calls == 1


async def test_retry_with_equivalent_timezone_offset_matches(
    note_store: NoteStore,
) -> None:
    first = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )
    shifted = SOURCE_AT.astimezone(timezone(timedelta(hours=7)))
    assert shifted == SOURCE_AT
    assert shifted.utcoffset() != SOURCE_AT.utcoffset()

    again = await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=shifted,
        identity=NoteIdentity(NOTE_A),
    )

    assert again == first
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_text_is_rejected(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text="Orchid demo moves to Monday.",
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_source_time_is_rejected(
    note_store: NoteStore,
) -> None:
    await remember_owner_note(
        note_store.memory,
        group_id=GROUP,
        text=BODY,
        source_at=SOURCE_AT,
        identity=NoteIdentity(NOTE_A),
    )

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=OLDER_AT,
            identity=NoteIdentity(NOTE_A),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 1


async def test_same_id_with_different_target_is_rejected(
    note_store: NoteStore,
) -> None:
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

    with pytest.raises(NoteConflictError, match="different payload") as caught:
        await remember_owner_note(
            note_store.memory,
            group_id=GROUP,
            text=BODY,
            source_at=SOURCE_AT,
            identity=NoteIdentity(NOTE_B),
        )

    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH
    assert note_store.extraction_calls == 2


async def test_ensure_note_indexes_requests_unique_uuid(
    note_store: NoteStore,
) -> None:
    await ensure_note_indexes(note_store.memory)
    await ensure_note_indexes(note_store.memory)

    assert note_store.indexes == 2
    assert note_store.queries[0][0] == owner_notes_module.INDEX_QUERY
    assert "REQUIRE n.uuid IS UNIQUE" in note_store.queries[0][0]
