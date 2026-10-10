import asyncio
import json
import signal
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

import fakeyou.cli.join as cli_join
import fakeyou.cli.memory as cli_memory
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.cli._operator import OperatorIdentity
from fakeyou.config.settings import Settings
from fakeyou.memory.persona import PersonaProfile
from fakeyou.observability.journal import journal_path
from fakeyou.persistence.record import load_meeting_record, meeting_record_path
from tests._cli_support import (
    MEET_LINK,
    PERSONA_DATA,
    _join_persona_conversation,
    fake_persona_memory,
    forbid_resources,
    printed_join_summary,
    write_brief,
    write_ingest_windows,
    write_persona,
    write_record,
)
from tests.fakes import (
    SessionRun,
    ToolCallingFakeModel,
    make_graphiti,
)


async def test_join_defers_ingest_then_memory_ingest_writes_episode(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.memory.recall.strategy = "tool"
    from fakeyou.persistence.ingestion_status import read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    created, audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("When is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert audio.spoken == ["The demo moves to Thursday."]
    assert graphiti.added == []
    assert graphiti.events == ["verify", "close"]
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "pending"
    assert status.record_sha256 is not None

    fake_persona_memory(monkeypatch, graphiti)
    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path, ingest_transcripts=True), None, None),
        twin_name=None,
        settings=settings,
    )

    [episode] = graphiti.added
    assert episode["episode_body"] == (
        "Speaker with unverified identity: When is the Orchid demo now?\n"
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

    await _join_persona_conversation(
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
    _created, first_audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=first_model,
        transcripts=("When does Orchid launch?",),
        brief_path=brief_path,
        native_checkpointer=True,
        settings=settings,
        join=join,
    )
    assert first_audio.spoken == ["Hello."]
    journal_file = journal_path(settings.paths.journal_dir, "meeting-9")
    journal_file.write_text(
        "".join(
            line
            for line in journal_file.read_text(encoding="utf-8").splitlines(keepends=True)
            if '"session_finished"' not in line and '"cli_exited"' not in line
        ),
        encoding="utf-8",
    )
    second_model = ToolCallingFakeModel(responses=[AIMessage(content="Again.")])
    _created, second_audio, _outcome = await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=second_model,
        transcripts=("Anything else?",),
        brief_path=brief_path,
        native_checkpointer=True,
        settings=settings,
        join=join,
        resume=True,
    )

    assert second_audio.spoken == ["Again."]
    first_turn = " ".join(str(message.content) for message in first_model.seen[0])
    second_turn = " ".join(str(message.content) for message in second_model.seen[0])
    assert "When does Orchid launch?" in first_turn
    assert "When does Orchid launch?" in second_turn
    assert len(second_model.seen[0]) > len(first_model.seen[0])
    entries = load_meeting_record(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    assert [entry.text for entry in entries if entry.kind == "twin_speech"] == [
        "Hello.",
        "Again.",
    ]
    assert [entry.kind for entry in entries].count("resume") == 1


async def test_join_resume_refuses_an_unreachable_lifecycle_checkpoint(
    join: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def unreachable(url):
        raise FakeYouError(ErrorCode.DATABASE_UNREACHABLE)
        yield

    monkeypatch.setattr(cli_join, "open_checkpointer", unreachable)
    forbid_resources(monkeypatch)

    with pytest.raises(FakeYouError) as caught:
        await join(MEET_LINK, resume=True)
    assert caught.value.code is ErrorCode.MEETING_LIFECYCLE_CHECKPOINT_UNCHECKABLE
    unreachable_message = str(FakeYouError(ErrorCode.DATABASE_UNREACHABLE))
    assert str(caught.value) == f"lifecycle checkpoint cannot be checked: {unreachable_message}"


async def test_memory_ingest_command_exits_1_when_ingestion_is_incomplete(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()

    async def failing_add(**kwargs):
        raise RuntimeError("graph down")

    graphiti.add_episode = failing_add
    fake_persona_memory(monkeypatch, graphiti)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1


async def test_memory_ingest_command_requires_a_record(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = fake_persona_memory(monkeypatch, make_graphiti())

    with pytest.raises(FakeYouError, match="no meeting record") as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_RECORD_MISSING
    assert opened == []


async def test_join_leaves_pending_status_with_hash_and_no_auto_ingest(
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import hashlib

    from fakeyou.persistence.ingestion_status import lock_path, read_status, status_path

    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="The demo moves to Thursday.")])

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        transcripts=("When is the Orchid demo now?",),
        persona_overrides={"ingest_transcripts": True},
        settings=settings,
        join=join,
    )

    assert graphiti.added == []
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    status = read_status(status_path(record_path))
    profile = PersonaProfile.model_validate({**PERSONA_DATA, "ingest_transcripts": True})
    assert status is not None
    assert status.status == "pending"
    assert status.group_id == profile.memory_group
    assert status.meeting_id == "meeting-9"
    assert status.record_sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
    assert status.windows == 1
    assert not lock_path(record_path).exists()
    printed = printed_join_summary(capsys)
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "pending"
    assert printed["record_error"] is None
    assert "memory-ingest --meeting-id meeting-9 --persona" in printed["resume_command"]
    assert "persona.json" in printed["resume_command"]
    assert "--owner-id settings-owner --twin-id settings-twin" in printed["resume_command"]


async def test_join_with_hanging_graphiti_leaves_without_ingesting(
    join: Callable[..., Any], settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graphiti = make_graphiti()
    graphiti.gate = asyncio.Event()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Noted.")])

    await asyncio.wait_for(
        _join_persona_conversation(
            tmp_path,
            monkeypatch,
            model=model,
            graphiti=graphiti,
            memory=True,
            transcripts=("When is the Orchid demo now?",),
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

    await _join_persona_conversation(
        tmp_path,
        monkeypatch,
        model=model,
        graphiti=graphiti,
        memory=True,
        settings=settings,
        join=join,
    )

    status = read_status(status_path(meeting_record_path(settings.paths.record_dir, "meeting-9")))
    assert status is not None
    assert status.status == "not_requested"
    assert status.record_sha256 is None
    printed = printed_join_summary(capsys)
    assert printed["ingestion_outcome"] == "not_requested"
    assert printed["resume_command"] is None


async def test_join_record_finalize_failure_is_visible_and_exits_1(
    session_run: SessionRun,
    join: Callable[..., Any],
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import os

    calls = 0

    def fail_after_lock_fsync(fd: int) -> None:
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

    def fail_at_record_close(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls > 3:
            raise OSError("disk unavailable")

    monkeypatch.setattr(os, "fsync", fail_at_record_close)
    graphiti = make_graphiti()
    model = ToolCallingFakeModel(responses=[AIMessage(content="Okay.")])

    with pytest.raises(SystemExit) as caught:
        await _join_persona_conversation(
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


async def test_memory_ingest_recovers_pending_without_hash_after_crash(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import hashlib

    from fakeyou.persistence.ingestion_status import (
        IngestionStatus,
        read_status,
        status_path,
        write_status,
    )

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    path = status_path(record_path)
    write_status(
        path,
        IngestionStatus.model_validate(
            {
                "meeting_id": "meeting-9",
                "group_id": profile.memory_group,
                "status": "pending",
                "windows": 0,
                "resumed_from": 0,
                "ingested": 0,
                "record_sha256": None,
                "updated_at": datetime(2026, 10, 3, 8, tzinfo=UTC).isoformat(),
                "reason": None,
            }
        ),
    )

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]
    status = read_status(path)
    assert status is not None
    assert status.status == "completed"
    assert status.record_sha256 == hashlib.sha256(record_path.read_bytes()).hexdigest()
    summary = json.loads(capsys.readouterr().out)
    assert (summary["ingested"], summary["completed"]) == (1, True)


async def test_memory_ingest_failure_then_resumes_from_native_progress(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_ingest_windows(settings, "meeting-9", 3)
    graphiti = make_graphiti(episode_prefix="uuid-")
    graphiti.errors[1] = RuntimeError("graph down")
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }

    with pytest.raises(SystemExit) as first:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert first.value.code == 1
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    failed = read_status(path)
    assert failed is not None
    assert failed.status == "failed"

    await cli_memory.ingest_recorded_meeting(**arguments)

    assert [episode["name"] for episode in graphiti.added] == [
        "meeting-9:w00000",
        "meeting-9:w00001",
        "meeting-9:w00002",
    ]
    resumed = read_status(path)
    assert resumed is not None
    assert (resumed.status, resumed.windows, resumed.resumed_from, resumed.ingested) == (
        "completed",
        3,
        1,
        2,
    )
    decoder = json.JSONDecoder()
    remaining = capsys.readouterr().out.strip()
    documents = []
    while remaining:
        document, end = decoder.raw_decode(remaining)
        documents.append(document)
        remaining = remaining[end:].strip()
    summary = documents[-1]
    assert (summary["ingested"], summary["completed"]) == (2, True)


async def test_memory_ingest_repeat_after_completed_adds_no_episodes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }

    await cli_memory.ingest_recorded_meeting(**arguments)
    await cli_memory.ingest_recorded_meeting(**arguments)

    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


async def test_memory_ingest_persists_window_episode_uuids(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_ingest_windows(settings, "meeting-9", 3)
    graphiti = make_graphiti(episode_prefix="uuid-")
    fake_persona_memory(monkeypatch, graphiti)

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    status = read_status(status_path(meeting_record_path(settings.paths.record_dir, "meeting-9")))
    assert status is not None
    assert status.episode_uuids == {
        0: "uuid-meeting-9:w00000",
        1: "uuid-meeting-9:w00001",
        2: "uuid-meeting-9:w00002",
    }


async def test_memory_ingest_merges_uuids_and_preserves_corrections_across_reingests(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path, write_status

    write_ingest_windows(settings, "meeting-9", 3)
    graphiti = make_graphiti(episode_prefix="uuid-")
    graphiti.errors[1] = RuntimeError("graph down")
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }

    with pytest.raises(SystemExit) as first:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert first.value.code == 1
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    failed = read_status(path)
    assert failed is not None
    assert failed.episode_uuids == {0: "uuid-meeting-9:w00000"}
    write_status(
        path,
        failed.model_copy(
            update={
                "corrected_at": "2026-10-09T12:00:00+00:00",
                "correction_episodes": ["uuid-fix"],
            }
        ),
    )

    await cli_memory.ingest_recorded_meeting(**arguments)

    resumed = read_status(path)
    assert resumed is not None
    assert resumed.episode_uuids == {
        0: "uuid-meeting-9:w00000",
        1: "uuid-meeting-9:w00001",
        2: "uuid-meeting-9:w00002",
    }
    assert resumed.corrected_at == "2026-10-09T12:00:00+00:00"
    assert resumed.correction_episodes == ["uuid-fix"]


async def test_memory_ingest_refuses_an_active_writer_lock(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import record_lock

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")

    with record_lock(record_path, purpose="meeting"):
        with pytest.raises(FakeYouError, match="locked") as caught:
            await cli_memory.ingest_recorded_meeting(
                meeting_id="meeting-9",
                identity=OperatorIdentity(write_persona(tmp_path), None, None),
                twin_name=None,
                settings=settings,
            )
    assert caught.value.code is ErrorCode.RECORD_LOCKED
    assert opened == []
    assert record_path.exists()


async def test_memory_ingest_rejects_a_changed_record_hash(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.foundation.transcript import TranscriptEvent
    from fakeyou.persistence.ingestion_status import read_status, status_path
    from fakeyou.persistence.record import MeetingRecord

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    arguments = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }
    await cli_memory.ingest_recorded_meeting(**arguments)

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    record = MeetingRecord(record_path)
    record.add_transcript(
        TranscriptEvent(
            id="late",
            timestamp="2026-09-30T11:00:00+00:00",
            source="meeting_audio",
            text="A late change.",
            finalized=True,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
    )
    record.close()

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert caught.value.code == 1
    assert len(graphiti.added) == 1
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "failed"


async def test_memory_ingest_rejects_a_foreign_group_status(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import (
        IngestionStatus,
        read_status,
        status_path,
        write_status,
    )

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    write_status(
        path,
        IngestionStatus.model_validate(
            {
                "meeting_id": "meeting-9",
                "group_id": "twin_other",
                "status": "pending",
                "windows": 1,
                "resumed_from": 0,
                "ingested": 0,
                "record_sha256": None,
                "updated_at": datetime(2026, 10, 3, 8, tzinfo=UTC).isoformat(),
                "reason": None,
            }
        ),
    )

    with pytest.raises(FakeYouError, match="group") as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH
    assert opened == []
    status = read_status(path)
    assert status is not None
    assert (status.status, status.group_id) == ("pending", "twin_other")


async def test_memory_ingest_corrupt_status_starts_fresh(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    path = status_path(meeting_record_path(settings.paths.record_dir, "meeting-9"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    assert len(graphiti.added) == 1
    status = read_status(path)
    assert status is not None
    assert status.status == "completed"


async def test_memory_ingest_partial_record_is_not_completed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write("{partial\n")

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1
    assert opened == []
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "failed"


async def test_memory_ingest_empty_record_reports_zero_windows(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path
    from fakeyou.persistence.record import MeetingRecord

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    MeetingRecord(record_path).close()
    fake_persona_memory(monkeypatch, make_graphiti())

    await cli_memory.ingest_recorded_meeting(
        meeting_id="meeting-9",
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    summary = json.loads(capsys.readouterr().out)
    assert (summary["windows"], summary["ingested"], summary["completed"]) == (0, 0, True)
    status = read_status(status_path(record_path))
    assert status is not None
    assert (status.status, status.windows) == ("completed", 0)


async def test_memory_ingest_stop_reports_interrupted_and_exits_1(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    real_ingest = cli_memory.ingest_until_stopped

    async def stop_first(*args, **kwargs):
        run = kwargs["run"]
        stop = run.stop if run.stop is not None else asyncio.Event()
        stop.set()
        kwargs["run"] = cli_memory.IngestRun(run.ingestion, stop)
        return await real_ingest(*args, **kwargs)

    monkeypatch.setattr(cli_memory, "ingest_until_stopped", stop_first)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id="meeting-9",
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1
    assert graphiti.added == []
    status = read_status(status_path(meeting_record_path(settings.paths.record_dir, "meeting-9")))
    assert status is not None
    assert status.status == "interrupted"


async def test_ingest_until_stopped_uses_the_caller_stop_event(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.observability.faults import FaultReporter
    from tests.fakes import RecordingJournal

    write_record(settings, "meeting-9")
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)
    before = signal.getsignal(signal.SIGINT)

    stopped = await cli_memory.ingest_until_stopped(
        make_graphiti(),
        cli_memory.IngestJob(record_path, profile, "meeting-9", "Rani Twin"),
        run=cli_memory.IngestRun(settings.memory.ingestion, asyncio.Event()),
        journal=journal,
        faults=faults,
    )
    assert (stopped.ingested, stopped.completed) == (1, True)

    halted = asyncio.Event()
    halted.set()
    early = await cli_memory.ingest_until_stopped(
        make_graphiti(),
        cli_memory.IngestJob(record_path, profile, "meeting-9", "Rani Twin"),
        run=cli_memory.IngestRun(settings.memory.ingestion, halted),
        journal=journal,
        faults=faults,
    )
    assert (early.ingested, early.completed) == (0, False)
    assert signal.getsignal(signal.SIGINT) == before
    journal.close()
