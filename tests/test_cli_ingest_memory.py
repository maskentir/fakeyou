import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

import pytest

import fakeyou.cli.memory as cli_memory
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.memory import ingest_service
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.persistence.record import meeting_record_path
from tests._cli_support import (
    PERSONA_DATA,
    fake_persona_memory,
    write_ingest_windows,
    write_persona,
    write_record,
)
from tests._error_support import message_pattern
from tests._memory_fakes import make_graphiti


async def test_memory_ingest_command_exits_1_when_ingestion_is_incomplete(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_record(settings, "meeting-9")
    graphiti = make_graphiti()

    async def failing_add(**_kwargs: Any) -> NoReturn:
        raise RuntimeError("graph down")

    graphiti.add_episode = failing_add
    fake_persona_memory(monkeypatch, graphiti)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id=SessionId("meeting-9"),
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
            meeting_id=SessionId("meeting-9"),
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_RECORD_MISSING
    assert opened == []


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
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
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
        meeting_id=SessionId("meeting-9"),
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
    arguments: dict[str, Any] = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }

    with pytest.raises(SystemExit) as first:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert first.value.code == 1
    path = status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
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
    arguments: dict[str, Any] = {
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
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    status = read_status(
        status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
    )
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
    arguments: dict[str, Any] = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }

    with pytest.raises(SystemExit) as first:
        await cli_memory.ingest_recorded_meeting(**arguments)
    assert first.value.code == 1
    path = status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
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
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))

    with record_lock(record_path, purpose="meeting"):
        with pytest.raises(FakeYouError, match="locked") as caught:
            await cli_memory.ingest_recorded_meeting(
                meeting_id=SessionId("meeting-9"),
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
    arguments: dict[str, Any] = {
        "meeting_id": "meeting-9",
        "identity": OperatorIdentity(write_persona(tmp_path), None, None),
        "twin_name": None,
        "settings": settings,
    }
    await cli_memory.ingest_recorded_meeting(**arguments)

    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
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
    path = status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
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
            meeting_id=SessionId("meeting-9"),
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code is ErrorCode.MEMORY_INGESTION_GROUP_MISMATCH
    assert opened == []
    status = read_status(path)
    assert status is not None
    assert (status.status, status.group_id) == ("pending", "twin_other")


async def test_memory_ingest_refuses_a_corrupt_status_until_it_is_inspected(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import status_path

    write_record(settings, "meeting-9")
    graphiti = make_graphiti()
    fake_persona_memory(monkeypatch, graphiti)
    path = status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt", encoding="utf-8")

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)
    ) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id=SessionId("meeting-9"),
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )

    assert caught.value.code is ErrorCode.INGESTION_STATUS_CORRUPT
    assert graphiti.added == []
    assert path.read_text(encoding="utf-8") == "{corrupt"
    journal_lines = (
        (settings.paths.journal_dir / "meeting-9.jsonl").read_text(encoding="utf-8").splitlines()
    )
    rows = [json.loads(line) for line in journal_lines if line.strip()]
    assert "ingestion_status_rejected" in [row["event"] for row in rows]


async def test_memory_ingest_partial_record_is_not_completed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fakeyou.persistence.ingestion_status import read_status, status_path

    write_record(settings, "meeting-9")
    opened = fake_persona_memory(monkeypatch, make_graphiti())
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    with record_path.open("a", encoding="utf-8") as handle:
        handle.write("{partial\n")

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id=SessionId("meeting-9"),
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

    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    MeetingRecord(record_path).close()
    fake_persona_memory(monkeypatch, make_graphiti())

    await cli_memory.ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
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
    real_ingest = ingest_service.ingest_until_stopped

    async def stop_first(*args: Any, **kwargs: Any) -> Any:
        run = kwargs["run"]
        stop = run.stop if run.stop is not None else asyncio.Event()
        stop.set()
        kwargs["run"] = ingest_service.IngestRun(run.ingestion, stop)
        return await real_ingest(*args, **kwargs)

    monkeypatch.setattr(ingest_service, "ingest_until_stopped", stop_first)

    with pytest.raises(SystemExit) as caught:
        await cli_memory.ingest_recorded_meeting(
            meeting_id=SessionId("meeting-9"),
            identity=OperatorIdentity(write_persona(tmp_path), None, None),
            twin_name=None,
            settings=settings,
        )
    assert caught.value.code == 1
    assert graphiti.added == []
    status = read_status(
        status_path(meeting_record_path(settings.paths.record_dir, SessionId("meeting-9")))
    )
    assert status is not None
    assert status.status == "interrupted"


async def test_ingest_until_stopped_uses_the_caller_stop_event(
    settings: Settings,
) -> None:
    from fakeyou.observability.faults import FaultReporter
    from tests._persistence_fakes import RecordingJournal

    write_record(settings, "meeting-9")
    profile = PersonaProfile.model_validate(PERSONA_DATA)
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    stopped = await ingest_service.ingest_until_stopped(
        make_graphiti(),
        ingest_service.IngestJob(record_path, profile, "meeting-9", "Rani Twin"),
        run=ingest_service.IngestRun(settings.memory.ingestion, asyncio.Event()),
        journal=journal,
        faults=faults,
    )
    assert (stopped.ingested, stopped.completed) == (1, True)

    halted = asyncio.Event()
    halted.set()
    early = await ingest_service.ingest_until_stopped(
        make_graphiti(),
        ingest_service.IngestJob(record_path, profile, "meeting-9", "Rani Twin"),
        run=ingest_service.IngestRun(settings.memory.ingestion, halted),
        journal=journal,
        faults=faults,
    )
    assert (early.ingested, early.completed) == (0, False)
    journal.close()
