import ast
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from graphiti_core.nodes import EpisodeType

import fakeyou.memory.owner_notes as owner_notes_module
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.settings import Settings
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.owner_note_service import (
    NoteCommand,
    apply_note,
    apply_note_for,
    forget_note_for,
    note_conflict,
)
from fakeyou.memory.owner_notes import (
    NOTE_CONFLICT_CODES,
    OWNER_NOTE_SOURCE,
    NoteConflictError,
    NoteIdentity,
    OwnerNoteResult,
    ensure_note_indexes,
    read_note_states,
    remember_owner_note,
)
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from tests._cli_support import write_persona
from tests._error_support import message_pattern
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


async def test_apply_note_resumes_the_same_operation_id(note_store: NoteStore) -> None:
    command = NoteCommand(text=BODY, source_at=SOURCE_AT, note_id=NOTE_A)

    first = await apply_note(note_store.memory, group_id=GROUP, command=command)
    second = await apply_note(note_store.memory, group_id=GROUP, command=command)

    assert first == second
    assert len(note_store.episodes) == 1


async def test_apply_note_payload_mismatch_is_a_structured_conflict(note_store: NoteStore) -> None:
    await apply_note(
        note_store.memory,
        group_id=GROUP,
        command=NoteCommand(text=BODY, source_at=SOURCE_AT, note_id=NOTE_A),
    )

    with pytest.raises(
        NoteConflictError, match=message_pattern(ErrorCode.NOTE_PAYLOAD_MISMATCH)
    ) as caught:
        await apply_note(
            note_store.memory,
            group_id=GROUP,
            command=NoteCommand(text="Different body.", source_at=SOURCE_AT, note_id=NOTE_A),
        )

    conflict = note_conflict(caught.value, episode_uuid=str(NOTE_A))
    assert conflict.code == ErrorCode.NOTE_PAYLOAD_MISMATCH.value
    assert conflict.episode_uuid == str(NOTE_A)
    assert conflict.retryable is False
    assert caught.value.code in NOTE_CONFLICT_CODES


async def test_apply_note_corrects_the_superseded_note(note_store: NoteStore) -> None:
    await apply_note(
        note_store.memory,
        group_id=GROUP,
        command=NoteCommand(text=BODY, source_at=OLDER_AT, note_id=NOTE_A),
    )

    result = await apply_note(
        note_store.memory,
        group_id=GROUP,
        command=NoteCommand(
            text="Orchid demo moves to Monday.",
            source_at=SOURCE_AT,
            note_id=NOTE_B,
            supersedes=NOTE_A,
        ),
    )

    assert result.supersedes == str(NOTE_A)
    assert note_store.controls[str(NOTE_A)]["status"] == "superseded"


@pytest.mark.parametrize(
    ("text", "source_at", "code"),
    [
        ("   ", SOURCE_AT, ErrorCode.MEMORY_NOTE_TEXT_BLANK),
        (BODY, datetime(2026, 9, 30, 6, 40), ErrorCode.MEMORY_SOURCE_AT_NAIVE),
        (BODY, datetime.now(UTC) + timedelta(days=1), ErrorCode.MEMORY_SOURCE_AT_FUTURE),
    ],
)
async def test_apply_note_guards_text_and_source_before_touching_memory(
    note_store: NoteStore, text: str, source_at: datetime, code: ErrorCode
) -> None:
    with pytest.raises(FakeYouError, match=message_pattern(code)) as caught:
        await apply_note(
            note_store.memory,
            group_id=GROUP,
            command=NoteCommand(text=text, source_at=source_at, note_id=NOTE_A),
        )

    assert caught.value.code is code
    assert note_store.queries == []


@pytest.mark.parametrize("code", sorted(NOTE_CONFLICT_CODES, key=str))
def test_note_conflict_is_retryable_only_for_a_concurrent_correction(code: ErrorCode) -> None:
    conflict = note_conflict(NoteConflictError(code), episode_uuid="ep-1")

    assert conflict.code == code.value
    assert conflict.episode_uuid == "ep-1"
    assert conflict.retryable is (code is ErrorCode.NOTE_CORRECTION_CONCURRENT)


def test_note_conflict_codes_are_exactly_the_codes_raised_as_conflicts() -> None:
    tree = ast.parse(Path(owner_notes_module.__file__).read_text(encoding="utf-8"))
    raised = {
        ErrorCode[node.args[0].attr]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "NoteConflictError"
        and node.args
        and isinstance(node.args[0], ast.Attribute)
    }

    assert raised == NOTE_CONFLICT_CODES
    assert len(NOTE_CONFLICT_CODES) == 6


def _note_access(settings: Settings, tmp_path: Path, note_store: NoteStore) -> OperatorAccess:
    @asynccontextmanager
    async def open_memory() -> AsyncIterator[Any]:
        yield note_store.memory

    return OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None), settings, open_memory
    )


async def test_apply_note_for_and_forget_note_for_use_the_persona_group(
    note_store: NoteStore, settings: Settings, tmp_path: Path
) -> None:
    access = _note_access(settings, tmp_path, note_store)
    group = PersonaProfile.model_validate_json(write_persona(tmp_path).read_bytes()).memory_group

    result = await apply_note_for(
        access, NoteCommand(text=BODY, source_at=SOURCE_AT, note_id=NOTE_A)
    )
    episode = note_store.episodes[str(NOTE_A)]
    await forget_note_for(access, result.episode_uuid)

    assert result.episode_uuid == str(NOTE_A)
    assert episode.group_id == group
    assert note_store.removed == [str(NOTE_A)]
    assert str(NOTE_A) not in note_store.controls


async def test_apply_note_for_rejects_blank_text_without_opening_memory(
    settings: Settings, tmp_path: Path
) -> None:
    opened: list[str] = []

    @asynccontextmanager
    async def open_memory() -> AsyncIterator[Any]:
        opened.append("memory")
        yield None

    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None), settings, open_memory
    )

    with pytest.raises(FakeYouError, match=message_pattern(ErrorCode.MEMORY_NOTE_TEXT_BLANK)):
        await apply_note_for(access, NoteCommand(text=" ", source_at=None, note_id=NOTE_A))

    assert opened == []
