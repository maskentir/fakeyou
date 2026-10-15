import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

import pytest

import fakeyou.cli.memory as cli_memory
import fakeyou.memory.memory as memory_module
import fakeyou.memory.owner_note_service as owner_note_service
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli.memory import CorrectionTarget
from fakeyou.config.settings import Settings
from fakeyou.memory import maintenance
from fakeyou.memory.maintenance import initialize_indexes, run_reembed
from fakeyou.memory.memory import ReembedResult
from fakeyou.memory.operator_access import OperatorAccess
from fakeyou.memory.owner_note_service import NoteRequest, add_note, correct_note
from fakeyou.memory.owner_notes import NoteConflictError, NoteIdentity, OwnerNoteResult
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from tests._cli_memory_support import tracked_memory
from tests._cli_support import PERSONA_DATA, fake_persona_memory, write_persona
from tests._memory_fakes import make_graphiti


@pytest.mark.asyncio
async def test_memory_init_builds_indexes(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    events: list[str] = []
    memory = make_graphiti()
    memory.events = events

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    await cli_memory.initialize_memory(settings)

    assert events == ["build", "close"]
    assert "Graphiti indexes initialized." in capsys.readouterr().out


@pytest.mark.asyncio
async def test_memory_init_closes_memory_when_index_setup_fails(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    memory = make_graphiti(fail_build=True)
    memory.events = events

    monkeypatch.setattr(memory_module, "create_graphiti_memory", lambda _config, **_kwargs: memory)

    with pytest.raises(RuntimeError, match="index setup failed"):
        await cli_memory.initialize_memory(settings)

    assert events == ["build", "close"]


async def test_memory_add_stores_owner_note_in_persona_group(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graphiti = make_graphiti()
    opened = fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded_at = datetime(2026, 10, 2, 8, tzinfo=UTC)
    note_id = UUID("b322d326-37b5-482a-904c-ef982f4dc510")

    async def remember(memory: Any, **kwargs: Any) -> OwnerNoteResult:
        calls.append({"memory": memory, **kwargs})
        return OwnerNoteResult(
            episode_uuid=str(note_id),
            facts=("The Orchid demo moves to Friday.",),
            source_at=source_at,
            recorded_at=recorded_at,
            supersedes=None,
        )

    monkeypatch.setattr(owner_note_service, "remember_owner_note", remember)

    await cli_memory.add_owner_note(
        "The Orchid demo moves to Friday.",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        settings=settings,
        source_at=source_at,
        note_id=note_id,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {
            "memory": graphiti,
            "group_id": profile.memory_group,
            "text": "The Orchid demo moves to Friday.",
            "source_at": source_at,
            "identity": NoteIdentity(note_id),
        }
    ]
    assert opened == ["open", "close"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "episode_uuid": str(note_id),
        "facts": ["The Orchid demo moves to Friday."],
        "source_at": source_at.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "supersedes": None,
    }
    assert captured.err == ""


async def test_memory_add_generates_and_reports_note_id_before_extraction(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    seen: list[dict[str, Any]] = []

    async def remember(_memory: Any, **kwargs: Any) -> OwnerNoteResult:
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["identity"].note_id),
            facts=("A note.",),
            source_at=datetime(2026, 9, 30, 6, 40, tzinfo=UTC),
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=None,
        )

    monkeypatch.setattr(owner_note_service, "remember_owner_note", remember)

    await cli_memory.add_owner_note(
        "A note.",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        settings=settings,
    )

    [call] = seen
    assert UUID(str(call["identity"].note_id)) == call["identity"].note_id
    assert f"note-id {call['identity'].note_id}" in capsys.readouterr().err


async def test_memory_add_rejects_future_source_before_opening_memory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="future") as caught:
        await cli_memory.add_owner_note(
            "A note.",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            settings=settings,
            source_at=datetime(2999, 1, 1, tzinfo=UTC),
        )
    assert caught.value.code is ErrorCode.MEMORY_SOURCE_AT_FUTURE
    assert opened == []


async def test_memory_add_propagates_a_note_conflict(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_persona_memory(monkeypatch, make_graphiti())

    async def conflicting(_memory: Any, **_kwargs: Any) -> NoReturn:
        raise NoteConflictError(ErrorCode.NOTE_PAYLOAD_MISMATCH)

    monkeypatch.setattr(owner_note_service, "remember_owner_note", conflicting)

    with pytest.raises(FakeYouError, match="already in use") as caught:
        await cli_memory.add_owner_note(
            "A note.",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            settings=settings,
        )
    assert caught.value.code is ErrorCode.NOTE_PAYLOAD_MISMATCH


async def test_memory_correct_supersedes_target_note(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graphiti = make_graphiti()
    opened = fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []
    target = UUID("a1b2c3d4-0000-4000-8000-000000000001")
    note_id = UUID("a1b2c3d4-0000-4000-8000-000000000002")
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    recorded_at = datetime(2026, 10, 2, 8, tzinfo=UTC)

    async def remember(memory: Any, **kwargs: Any) -> OwnerNoteResult:
        calls.append({"memory": memory, **kwargs})
        return OwnerNoteResult(
            episode_uuid=str(note_id),
            facts=("The Orchid demo moves to Friday.",),
            source_at=source_at,
            recorded_at=recorded_at,
            supersedes=str(target),
        )

    monkeypatch.setattr(owner_note_service, "remember_owner_note", remember)

    await cli_memory.correct_owner_note(
        "The Orchid demo moves to Friday.",
        target=CorrectionTarget(str(target), note_id),
        source_at=source_at,
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {
            "memory": graphiti,
            "group_id": profile.memory_group,
            "text": "The Orchid demo moves to Friday.",
            "source_at": source_at,
            "identity": NoteIdentity(note_id, target),
        }
    ]
    assert opened == ["open", "close"]
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "episode_uuid": str(note_id),
        "facts": ["The Orchid demo moves to Friday."],
        "source_at": source_at.isoformat(),
        "recorded_at": recorded_at.isoformat(),
        "supersedes": str(target),
    }
    assert captured.err == ""


async def test_memory_correct_generates_note_id_when_missing(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_persona_memory(monkeypatch, make_graphiti())
    seen: list[dict[str, Any]] = []
    target = UUID("a1b2c3d4-0000-4000-8000-000000000001")
    source_at = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)

    async def remember(_memory: Any, **kwargs: Any) -> OwnerNoteResult:
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["identity"].note_id),
            facts=("Fixed.",),
            source_at=source_at,
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=str(target),
        )

    monkeypatch.setattr(owner_note_service, "remember_owner_note", remember)

    await cli_memory.correct_owner_note(
        "Fixed.",
        target=CorrectionTarget(str(target), None),
        source_at=source_at,
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        settings=settings,
    )

    [call] = seen
    assert call["identity"].supersedes == target
    assert f"note-id {call['identity'].note_id}" in capsys.readouterr().err


CORRECT_TARGET = "a1b2c3d4-0000-4000-8000-000000000001"


CORRECT_SOURCE = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)


@pytest.mark.parametrize(
    "case",
    [
        ("  ", CORRECT_TARGET, CORRECT_SOURCE, ErrorCode.MEMORY_NOTE_TEXT_BLANK, "blank"),
        ("Fixed.", "not-a-uuid", CORRECT_SOURCE, ErrorCode.MEMORY_EPISODE_NOT_UUID, "UUID"),
        (
            "Fixed.",
            CORRECT_TARGET,
            datetime(2999, 1, 1, tzinfo=UTC),
            ErrorCode.MEMORY_SOURCE_AT_FUTURE,
            "future",
        ),
        (
            "Fixed.",
            CORRECT_TARGET,
            datetime(2026, 9, 30, 6, 40),
            ErrorCode.MEMORY_SOURCE_AT_NAIVE,
            "timezone",
        ),
    ],
)
async def test_memory_correct_validates_before_opening_memory(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, datetime, ErrorCode, str],
) -> None:
    text, episode_uuid, source_at, code, match = case
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match=match) as caught:
        await cli_memory.correct_owner_note(
            text,
            target=CorrectionTarget(episode_uuid, None),
            source_at=source_at,
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            settings=settings,
        )
    assert caught.value.code is code
    assert opened == []


async def test_memory_correct_rejects_a_foreign_identity_before_opening_memory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="owner_id and twin_id"):
        await cli_memory.correct_owner_note(
            "Fixed.",
            target=CorrectionTarget(CORRECT_TARGET, None),
            source_at=CORRECT_SOURCE,
            identity=OperatorIdentity(write_persona(tmp_path), "other-owner", None),
            settings=settings,
        )
    assert opened == []


async def test_memory_add_validates_before_opening_memory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="blank") as caught:
        await cli_memory.add_owner_note(
            "  ",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_NOTE_TEXT_BLANK
    assert opened == []


async def test_memory_add_rejects_a_foreign_identity_before_opening_memory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="owner_id and twin_id"):
        await cli_memory.add_owner_note(
            "A note.",
            identity=OperatorIdentity(write_persona(tmp_path), "other-owner", None),
            settings=settings,
        )
    assert opened == []


async def test_add_note_announces_only_a_generated_note_id(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def remember(_memory: Any, **_kwargs: Any) -> OwnerNoteResult:
        return OwnerNoteResult(
            episode_uuid="episode-1",
            facts=("A note.",),
            source_at=datetime(2026, 9, 30, 6, 40, tzinfo=UTC),
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=None,
        )

    monkeypatch.setattr(owner_note_service, "remember_owner_note", remember)
    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: tracked_memory([]),
    )
    announced: list[UUID] = []
    given = UUID("b322d326-37b5-482a-904c-ef982f4dc510")

    await add_note(NoteRequest("A note.", None, given), access=access, announce=announced.append)
    assert announced == []

    await add_note(NoteRequest("A note.", None), access=access, announce=announced.append)
    [generated] = announced
    assert isinstance(generated, UUID)


async def test_correct_note_rejects_a_non_uuid_target_before_opening_memory(
    settings: Settings, tmp_path: Path
) -> None:
    opened: list[str] = []
    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: tracked_memory(opened),
    )

    with pytest.raises(FakeYouError, match="UUID") as caught:
        await correct_note(
            NoteRequest("Fixed.", CORRECT_SOURCE),
            supersedes="not-a-uuid",
            access=access,
            announce=lambda _operation: None,
        )
    assert caught.value.code is ErrorCode.MEMORY_EPISODE_NOT_UUID
    assert opened == []


async def test_initialize_indexes_builds_indexes_through_the_opened_memory() -> None:
    events: list[str] = []
    memory = make_graphiti()
    memory.events = events

    @asynccontextmanager
    async def open_memory() -> AsyncIterator[Any]:
        yield memory

    await initialize_indexes(open_memory)

    assert events == ["build"]


async def test_run_reembed_counts_the_operator_group(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    async def reembed(_memory: Any, *, group_id: str) -> ReembedResult:
        seen.append(group_id)
        return ReembedResult(entities=2, facts=5)

    monkeypatch.setattr(maintenance, "reembed_group", reembed)
    access = OperatorAccess(
        OperatorIdentity(write_persona(tmp_path), None, None),
        settings,
        lambda: tracked_memory([]),
    )

    result = await run_reembed(access)

    assert result == ReembedResult(entities=2, facts=5)
    assert seen == [PersonaProfile.model_validate(PERSONA_DATA).memory_group]
