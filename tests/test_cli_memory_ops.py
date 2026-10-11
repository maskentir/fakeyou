import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID

import pytest
from pydantic import SecretStr

import fakeyou.cli.main as cli_main
import fakeyou.cli.memory as cli_memory
import fakeyou.memory.memory as memory_module
import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli._operator import OperatorIdentity
from fakeyou.cli.memory import CorrectionTarget
from fakeyou.config.settings import (
    EmbedderSettings,
    Settings,
)
from fakeyou.conversation.conversation import conversation_thread_id
from fakeyou.db.checkpoint import open_checkpointer
from fakeyou.db.engine import configured_url, require_url
from fakeyou.memory import owner_notes
from fakeyou.memory.owner_notes import NoteConflictError, NoteIdentity, OwnerNoteResult
from fakeyou.memory.persona import PersonaProfile
from fakeyou.persistence.brief import (
    context_path,
    read_meeting_context,
    write_meeting_context,
)
from fakeyou.persistence.ingestion_status import LockPurpose, record_lock
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import (
    PERSONA_DATA,
    _thread_exists,
    expected_context,
    fake_persona_memory,
    meeting_brief,
    meeting_context_file,
    seed_conversation_thread,
    write_persona,
)
from tests.fakes import (
    make_graphiti,
)


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

    assert cli_memory.remember_owner_note is owner_notes.remember_owner_note
    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

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

    async def remember(memory: Any, **kwargs: Any) -> OwnerNoteResult:
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["identity"].note_id),
            facts=("A note.",),
            source_at=datetime(2026, 9, 30, 6, 40, tzinfo=UTC),
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=None,
        )

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

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

    monkeypatch.setattr(cli_memory, "remember_owner_note", conflicting)

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

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

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

    async def remember(memory: Any, **kwargs: Any) -> OwnerNoteResult:
        seen.append(kwargs)
        return OwnerNoteResult(
            episode_uuid=str(kwargs["identity"].note_id),
            facts=("Fixed.",),
            source_at=source_at,
            recorded_at=datetime(2026, 10, 2, 8, tzinfo=UTC),
            supersedes=str(target),
        )

    monkeypatch.setattr(cli_memory, "remember_owner_note", remember)

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


_CORRECT_TARGET = "a1b2c3d4-0000-4000-8000-000000000001"

_CORRECT_SOURCE = datetime(2026, 9, 30, 6, 40, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "episode_uuid", "source_at", "code", "match"),
    [
        ("  ", _CORRECT_TARGET, _CORRECT_SOURCE, ErrorCode.MEMORY_NOTE_TEXT_BLANK, "blank"),
        ("Fixed.", "not-a-uuid", _CORRECT_SOURCE, ErrorCode.MEMORY_EPISODE_NOT_UUID, "UUID"),
        (
            "Fixed.",
            _CORRECT_TARGET,
            datetime(2999, 1, 1, tzinfo=UTC),
            ErrorCode.MEMORY_SOURCE_AT_FUTURE,
            "future",
        ),
        (
            "Fixed.",
            _CORRECT_TARGET,
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
    text: str,
    episode_uuid: str,
    source_at: datetime,
    code: ErrorCode,
    match: str,
) -> None:
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
            target=CorrectionTarget(_CORRECT_TARGET, None),
            source_at=_CORRECT_SOURCE,
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


async def test_memory_forget_meeting_uses_persona_group(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []

    async def forget(memory: Any, **kwargs: Any) -> int:
        calls.append({"memory": memory, **kwargs})
        return 3

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)

    await cli_memory.forget_memory(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [
        {"memory": graphiti, "group_id": profile.memory_group, "meeting_id": "meeting-9"}
    ]
    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 3,
        "record_removed": False,
        "journal_files_removed": 0,
        "conversation_thread_removed": False,
    }


async def _forget_meeting_with_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, settings: Settings, keep_record: bool
) -> tuple[Settings, str]:
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n")
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        record_path.with_name(artifact).write_text("synthetic artifact")
    write_meeting_context(context_path(record_path), expected_context(meeting_brief(), profile))
    settings.paths.journal_dir.mkdir()
    for name in ("meeting-9.jsonl", "meeting-9.jsonl.1", "meeting-10.jsonl"):
        (settings.paths.journal_dir / name).write_text("{}\n")
    thread_id = await seed_conversation_thread(settings, "meeting-9")
    await seed_conversation_thread(settings, "meeting-10")
    from fakeyou.orchestration.lifecycle import MeetingLifecycle

    async with open_checkpointer(require_url(configured_url(settings))) as saver:
        for meeting in ("meeting-9", "meeting-10"):
            lifecycle = MeetingLifecycle(profile.owner_id, profile.twin_id, meeting, saver)
            await lifecycle.initialize()
    fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(memory: Any, **kwargs: Any) -> int:
        return 2

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)
    await cli_memory.forget_memory(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id="meeting-9",
        episode_uuid=None,
        settings=settings,
        keep_record=keep_record,
    )
    return settings, thread_id


async def test_memory_forget_meeting_removes_record_journal_and_thread(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {
        "removed_episodes": 2,
        "record_removed": True,
        "journal_files_removed": 2,
        "conversation_thread_removed": True,
    }
    assert not (settings.paths.record_dir / "meeting-9").exists()
    assert sorted(path.name for path in settings.paths.journal_dir.iterdir()) == [
        "meeting-10.jsonl"
    ]
    assert not await _thread_exists(settings, thread_id)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    other = conversation_thread_id(profile.owner_id, profile.twin_id, "meeting-10")
    assert await _thread_exists(settings, other)


async def test_memory_forget_meeting_keep_record_only_removes_episodes(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=True, settings=settings
    )

    assert json.loads(capsys.readouterr().out) == {"removed_episodes": 2}
    assert meeting_record_path(settings.paths.record_dir, "meeting-9").exists()
    assert (settings.paths.journal_dir / "meeting-9.jsonl").exists()
    assert await _thread_exists(settings, thread_id)
    for artifact in ("debrief.json", "debrief.md", "debrief-attempt.json"):
        assert (
            meeting_record_path(settings.paths.record_dir, "meeting-9").with_name(artifact).exists()
        )


@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_keeps_the_meeting_context_only_with_the_record(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_record: bool
) -> None:
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    context_file = meeting_context_file(settings)

    await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=keep_record, settings=settings
    )

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    assert record_path.exists() is keep_record
    assert context_file.exists() is keep_record
    if keep_record:
        assert read_meeting_context(context_file) == expected_context(meeting_brief(), profile)


async def test_memory_forget_episode_uses_persona_group(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    calls: list[dict[str, Any]] = []

    async def forget(memory: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(cli_memory, "forget_episode", forget)

    await cli_memory.forget_memory(
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        meeting_id=None,
        episode_uuid="episode-7",
        settings=settings,
    )

    profile = PersonaProfile.model_validate(PERSONA_DATA)
    assert calls == [{"group_id": profile.memory_group, "episode_uuid": "episode-7"}]


@pytest.mark.parametrize(
    ("meeting_id", "episode_uuid"),
    [(None, None), ("meeting-9", "episode-7")],
)
async def test_memory_forget_validates_before_opening_memory(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    meeting_id: str | None,
    episode_uuid: str | None,
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="exactly one") as caught:
        await cli_memory.forget_memory(
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            meeting_id=meeting_id,
            episode_uuid=episode_uuid,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_FORGET_TARGET_REQUIRED
    assert opened == []


async def test_memory_forget_rejects_a_malformed_meeting_id_before_opening_memory(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(ValueError, match="String should match pattern"):
        await cli_memory.forget_memory(
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            meeting_id="bad meeting",
            episode_uuid=None,
            settings=settings,
        )
    assert opened == []


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (
            ["memory-add", "A note.", "--persona", "p.json", "--owner-id", "o", "--twin-id", "t"],
            "add_owner_note",
            {"identity": OperatorIdentity(Path("p.json"), "o", "t")},
        ),
        (
            ["memory-forget", "--meeting-id", "m1", "--persona", "p.json"],
            "forget_memory",
            {
                "identity": OperatorIdentity(Path("p.json"), None, None),
                "meeting_id": "m1",
                "episode_uuid": None,
            },
        ),
    ],
)
def test_memory_operator_commands_parse_arguments(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], target: str, expected: dict[str, Any]
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, target, fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert {key: captured[key] for key in expected} == expected


def test_memory_add_parses_optional_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-add",
            "A note.",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "b322d326-37b5-482a-904c-ef982f4dc510",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["note_id"] == UUID("b322d326-37b5-482a-904c-ef982f4dc510")


def test_memory_add_defaults_source_and_note_id_to_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "add_owner_note", fake_command)
    monkeypatch.setattr(sys, "argv", ["fakeyou", "memory-add", "A note.", "--persona", "p.json"])
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["source_at"] is None
    assert captured["note_id"] is None


def test_memory_correct_parses_target_source_and_note_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_command(*args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli_main, "correct_owner_note", fake_command)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fakeyou",
            "memory-correct",
            "Fixed note.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00+00:00",
            "--note-id",
            "a1b2c3d4-0000-4000-8000-000000000002",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        cli_main.main()

    assert exited.value.code == 0
    assert captured["text"] == "Fixed note."
    assert captured["target"] == CorrectionTarget(
        "a1b2c3d4-0000-4000-8000-000000000001",
        UUID("a1b2c3d4-0000-4000-8000-000000000002"),
    )
    assert captured["source_at"] == datetime(2026, 9, 30, 6, 40, tzinfo=UTC)
    assert captured["identity"].persona_path == Path("p.json")


@pytest.mark.parametrize(
    "argv",
    [
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "2026-09-30T06:40:00"],
        ["memory-add", "A note.", "--persona", "p.json", "--source-at", "not-a-timestamp"],
        ["memory-add", "A note.", "--persona", "p.json", "--note-id", "not-a-uuid"],
        [
            "memory-correct",
            "Fixed.",
            "--episode",
            "a1b2c3d4-0000-4000-8000-000000000001",
            "--persona",
            "p.json",
            "--source-at",
            "2026-09-30T06:40:00",
        ],
    ],
)
def test_memory_commands_reject_invalid_source_or_note_id_at_parse_time(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["fakeyou", *argv])
    with pytest.raises(SystemExit) as caught:
        cli_main.main()

    assert caught.value.code == 2


def test_memory_forget_rejects_both_targets_at_parse_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["fakeyou", "memory-forget", "--meeting-id", "m1", "--episode", "e1", "--persona", "p"],
    )
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 2


def test_memory_config_treats_a_blank_embedding_endpoint_as_the_default(settings: Settings) -> None:
    settings.memory.embedder = EmbedderSettings(base_url="", api_key=SecretStr("local"))
    assert settings.memory.embedder.base_url is None
    assert (
        providers.memory_config(settings).embedder.resolved_base_url == "https://api.openai.com/v1"
    )


def test_memory_config_routes_embeddings_and_extraction_separately(settings: Settings) -> None:
    settings.memory.embedder.base_url = "http://embeddings/v1"
    settings.memory.embedder.api_key = SecretStr("local")
    settings.memory.embedder.dim = 768
    settings.memory.llm.base_url = "https://api.meta.ai/v1"
    settings.memory.llm.api_key = SecretStr("meta-key")

    config = providers.memory_config(settings)
    assert config.embedder.api_key is not None
    assert config.llm.api_key is not None

    assert (
        config.embedder.resolved_base_url,
        config.embedder.api_key.get_secret_value(),
        config.embedder.dim,
    ) == (
        "http://embeddings/v1",
        "local",
        768,
    )
    assert (config.llm.resolved_base_url, config.llm.api_key.get_secret_value()) == (
        "https://api.meta.ai/v1",
        "meta-key",
    )


async def test_memory_forget_keep_record_requires_a_meeting(
    settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(FakeYouError, match="--keep-record") as caught:
        await cli_memory.forget_memory(
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            meeting_id=None,
            episode_uuid="episode-7",
            settings=settings,
            keep_record=True,
        )
    assert caught.value.code is ErrorCode.MEMORY_KEEP_RECORD_REQUIRES_MEETING


async def test_memory_forget_should_delete_lifecycle_and_preserve_other_meetings(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.orchestration.lifecycle import meeting_thread_id

    settings, thread_id = await _forget_meeting_with_local_files(
        tmp_path, monkeypatch, keep_record=False, settings=settings
    )
    profile = PersonaProfile.model_validate(PERSONA_DATA)

    assert not await _thread_exists(settings, thread_id)
    assert not await _thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-9"))
    assert await _thread_exists(settings, meeting_thread_id(profile.twin_id, "meeting-10"))


@pytest.mark.parametrize("lock_purpose", ["meeting", "ingestion", "debrief"])
@pytest.mark.parametrize("keep_record", [False, True])
async def test_memory_forget_meeting_refuses_an_active_lock(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep_record: bool,
    lock_purpose: LockPurpose,
) -> None:
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record_path.parent.mkdir(parents=True)
    record_path.write_text("{}\n", encoding="utf-8")
    context = expected_context(meeting_brief(), PersonaProfile.model_validate(PERSONA_DATA))
    write_meeting_context(context_path(record_path), context)
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    async def forget(memory: Any, **kwargs: Any) -> NoReturn:
        pytest.fail("provider must stay closed while the record is locked")

    monkeypatch.setattr(cli_memory, "forget_meeting", forget)

    with record_lock(record_path, purpose=lock_purpose):
        with pytest.raises(FakeYouError, match="locked") as caught:
            await cli_memory.forget_memory(
                identity=OperatorIdentity(write_persona(tmp_path), None, None),
                meeting_id="meeting-9",
                episode_uuid=None,
                settings=settings,
                keep_record=keep_record,
            )
    assert caught.value.code is ErrorCode.RECORD_LOCKED
    assert opened == []
    assert record_path.exists()
    assert read_meeting_context(context_path(record_path)) == context
