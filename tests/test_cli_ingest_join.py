import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import tests._join_runner as cli_join
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.ingestion_status import finalized_prefix
from fakeyou.persistence.record import load_meeting_record, meeting_record_path
from tests._cli_support import (
    MEET_LINK,
    PERSONA_DATA,
    fake_persona_memory,
    forbid_resources,
    join_persona_conversation,
    printed_join_summary,
    write_brief,
    write_persona,
)
from tests._conversation_fakes import ToolCallingFakeModel
from tests._error_support import message_pattern
from tests._ingest_support import ingest_recorded_meeting
from tests._memory_fakes import make_graphiti


async def test_join_defers_ingest_then_memory_ingest_writes_episode(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    from fakeyou.persistence.ingestion_status import read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    created, audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("Rani Twin, when is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert audio.spoken == ["The demo moves to Thursday."]
    assert graphiti.added == []
    assert graphiti.events == ["verify", "close"]
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"
    assert status.record_sha256 is not None

    fake_persona_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(write_persona(tmp_path, ingest_transcripts=True), None, None),
        twin_name=None,
        settings=settings,
    )

    [episode] = graphiti.added
    assert episode["episode_body"] == (
        "Speaker with unverified identity: Rani Twin, when is the Orchid demo now?\n"
        "Digital twin: The demo moves to Thursday."
    )
    assert episode["name"] == "meeting-9:w00000"
    assert episode["group_id"] == profile.memory_group
    assert episode["saga"] == "meeting:meeting-9"
    assert [tool.name for tool in created["tools"]] == ["recall_meeting_context"]


async def test_join_without_ingest_permission_never_writes_memory(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()

    await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=ToolCallingFakeModel(responses=[AIMessage(content="Okay.")]),
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    assert graphiti.added == []


async def test_join_resume_reuses_history_without_replaying_answers(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    brief_path = write_brief(tmp_path)
    first_model = ToolCallingFakeModel(responses=[AIMessage(content="Hello.")])
    _created, first_audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=first_model,
        transcripts=("Rani Twin, when does Orchid launch?",),
        brief_path=brief_path,
        settings=settings,
        join=join,
    )
    assert first_audio.spoken == ["Hello."]
    journal_file = journal_path(settings.paths.journal_dir, SessionId("meeting-9"))
    journal_file.write_text(
        "".join(
            line
            for line in journal_file.read_text(encoding="utf-8").splitlines(keepends=True)
            if '"session_finished"' not in line and '"cli_exited"' not in line
        ),
        encoding="utf-8",
    )
    second_model = ToolCallingFakeModel(responses=[AIMessage(content="Again.")])
    _created, second_audio, _outcome = await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=second_model,
        transcripts=("Rani Twin, anything else?",),
        brief_path=brief_path,
        settings=settings,
        join=join,
        resume=True,
    )

    assert second_audio.spoken == ["Again."]
    first_turn = " ".join(str(message.content) for message in first_model.seen[0])
    second_turn = " ".join(str(message.content) for message in second_model.seen[0])
    assert "Rani Twin, when does Orchid launch?" in first_turn
    assert "Rani Twin, when does Orchid launch?" in second_turn
    assert len(second_model.seen[0]) > len(first_model.seen[0])
    entries = load_meeting_record(
        meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    )
    assert [entry.text for entry in entries if entry.kind == "twin_speech"] == [
        "Hello.",
        "Again.",
    ]
    assert [entry.kind for entry in entries].count("resume") == 1


async def test_join_resume_refuses_an_unreachable_lifecycle_checkpoint(
    join: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def unreachable(url: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        del url
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)
        yield

    monkeypatch.setattr(cli_join, "open_checkpointer", unreachable)
    forbid_resources(monkeypatch)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE)
    ) as caught:
        await join(MEET_LINK, resume=True)
    assert caught.value.code is ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE
    assert str(caught.value) == (
        "lifecycle checkpoint cannot be checked: FakeYouError: "
        "PostgreSQL is unreachable; check FAKEYOU_DATABASE__URL and that the postgres "
        "service is up."
    )


async def test_join_leaves_pending_status_with_hash_and_no_auto_ingest(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:

    from fakeyou.persistence.ingestion_status import lock_path, read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("Rani Twin, when is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    assert graphiti.added == []
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    status = read_status(status_path(record_path))
    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert status is not None
    assert status.status == "pending"
    assert status.group_id == profile.memory_group
    assert status.meeting_id == "meeting-9"
    digest, byte_count = finalized_prefix(record_path)
    assert status.record_sha256 == digest
    assert status.record_bytes == byte_count
    assert status.windows == 1
    assert not lock_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "pending"
    assert printed["record_error"] is None
    assert printed["resume_command"] == "fakeyou memory-ingest --meeting-id meeting-9"


async def test_join_with_hanging_graphiti_leaves_without_ingesting(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    graphiti.gate = asyncio.Event()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Noted.")])

    await asyncio.wait_for(
        join_persona_conversation(
            tmp_path,
            monkeypatch,
            model=model,
            graphiti=graphiti,
            memory=True,
            transcripts=("Rani Twin, when is the Orchid demo now?",),
            persona_overrides={"ingest_transcripts": True},
            settings=settings,
            join=join,
        ),
        timeout=20,
    )

    assert graphiti.added == []


async def test_join_without_ingest_intent_writes_not_requested(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Okay.")])

    await join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    status = read_status(
        status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
    )
    assert status is not None
    assert status.status == "not_requested"
    assert status.record_sha256 is None
    printed = printed_join_summary(capsys)
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["resume_command"] is None


@pytest.mark.usefixtures("session_run")
async def test_join_record_finalize_failure_is_visible_and_exits_1(
    join: Callable[..., Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os

    calls = 0

    def fail_after_lock_fsync(_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", fail_after_lock_fsync)

    with pytest.raises(SystemExit) as caught:
        await join(MEET_LINK)
    assert caught.value.code == 1

    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "not_requested"
    assert "record" in printed["record_error"]
    rows = [
        json.loads(line)
        for line in (tmp_path / "journals" / "meeting-1.jsonl").read_text().splitlines()
    ]
    finished = [row for row in rows if row["event"] == "session_finished"]
    exited = [row for row in rows if row["event"] == "cli_exited"]
    assert [row["exit_code"] for row in finished] == [0]
    assert [row["exit_code"] for row in exited] == [1]


async def test_join_ingest_requested_but_finalize_failed_reports_unknown(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os

    calls = 0

    def fail_at_record_close(_fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", fail_at_record_close)
    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Okay.")])

    with pytest.raises(SystemExit) as caught:
        await join_persona_conversation(
            tmp_path,
            monkeypatch,
            model=model,
            graphiti=graphiti,
            memory=True,
            persona_overrides={"ingest_transcripts": True},
            settings=settings,
            join=join,
        )
    assert caught.value.code == 1

    printed = printed_join_summary(capsys)
    assert printed["ingestion_outcome"] == "unknown"
    assert "record" in printed["record_error"]
    assert printed["resume_command"] is None
