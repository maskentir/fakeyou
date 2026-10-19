import json
import struct
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import fakeyou.orchestration.providers as providers
from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.catalog.exit_codes import ExitCode
from fakeyou.config.identifiers import SessionId
from fakeyou.config.settings import Settings
from fakeyou.diarization.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.foundation.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.foundation.transcript import TranscriptEvent
from fakeyou.memory import ingest_service
from fakeyou.memory.ingest_status import IngestOutcome, finish_ingest
from fakeyou.memory.ingestion import EpisodeNameError, MeetingScope, ingest_corrections
from fakeyou.memory.persona import OperatorIdentity, PersonaProfile
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.orchestration.lifecycle import meeting_thread_id
from fakeyou.persistence.ingestion_status import (
    IngestionStatus,
    read_status,
    status_path,
    write_status,
)
from fakeyou.persistence.record import MeetingRecord, meeting_record_path
from tests._error_support import message_pattern
from tests._ingest_support import ingest_recorded_meeting
from tests._memory_fakes import make_graphiti
from tests._persistence_fakes import RecordingJournal

PERSONA_DATA = {
    "owner_id": "settings-owner",
    "twin_id": "settings-twin",
    "display_name": "Rani Twin",
    "role": "Product lead for the synthetic Orchid project",
    "verified_facts": ["The Orchid beta launch is planned for 14 October 2026"],
    "allowed_tools": ["recall_meeting_context"],
}


def _write_persona(tmp_path: Path) -> Path:
    path = tmp_path / "persona.json"
    path.write_text(json.dumps(PERSONA_DATA), encoding="utf-8")
    return path


def _write_record(settings: Settings, meeting_id: str) -> None:
    record = MeetingRecord(meeting_record_path(settings.paths.record_dir, SessionId(meeting_id)))
    record.add_transcript(
        TranscriptEvent(
            id="t1",
            timestamp="2026-09-30T10:00:00+00:00",
            source="meeting_audio",
            text="The Orchid vendor kickoff is on Tuesday.",
            finalized=True,
            participant_id="unknown",
            speaker_evidence_id=None,
        )
    )
    record.add_twin_speech("Got it, Tuesday.")
    record.close()


def _write_audio_slots(settings: Settings, meeting_id: str) -> Path:
    audio_dir = (
        meeting_record_path(settings.paths.record_dir, SessionId(meeting_id)).parent / "audio"
    )
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "slots.json").write_text(
        json.dumps(
            {
                "schema": "slot_audio/v2",
                "slots": {"0": {"seq_start": 0, "seq_end": 10, "frames": 1600, "gaps_filled": 0}},
            }
        ),
        encoding="utf-8",
    )
    samples = [100] * 1600
    with wave.open(str(audio_dir / "slot-0.wav"), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CAPTURE_SAMPLE_RATE)
        handle.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return audio_dir


def _stub_transcribe(monkeypatch: pytest.MonkeyPatch, turns: list[tuple[int, int, str]]) -> None:
    def fake(chunk: bytes, settings: Any, transport: Any = None) -> list[DiarizedTurn]:
        del chunk
        del transport
        del settings
        return [
            DiarizedTurn(start_ms=start, end_ms=end, speaker=name) for start, end, name in turns
        ]

    monkeypatch.setattr("fakeyou.diarization.diarize_service.transcribe_chunk", fake)


def _fake_memory(monkeypatch: pytest.MonkeyPatch, memory: Any) -> None:
    @asynccontextmanager
    async def open_memory(_settings: Settings) -> Any:
        yield memory

    monkeypatch.setattr(providers, "open_persona_memory", open_memory)


def _journal_rows(settings: Settings, meeting_id: str) -> list[dict[str, Any]]:
    path = settings.paths.journal_dir / f"{meeting_id}.jsonl"
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_finish_ingest_preserves_correction_progress(tmp_path: Path) -> None:
    status_file = tmp_path / "ingestion.json"
    write_status(
        status_file,
        IngestionStatus(
            meeting_id="meeting-1",
            group_id="twin_group",
            status="completed",
            windows=3,
            ingested=3,
            episode_uuids={0: "uuid-a"},
            corrected_at="2026-10-09T12:00:00+00:00",
            correction_episodes=["uuid-fix"],
            correction_windows={0: "uuid-fix"},
            correction_run="2026-10-09T12:00:00+00:00",
        ),
    )
    faults = FaultReporter(journal=Journal("meeting-1", echo=False))

    summary = finish_ingest(
        status_file,
        IngestOutcome(
            state="completed",
            windows=3,
            resumed_from=0,
            ingested=3,
            facts=1,
            completed=True,
            record_sha256="abc",
            reason=None,
            episode_uuids={1: "uuid-b"},
        ),
        meeting_id="meeting-1",
        group_id="twin_group",
        faults=faults,
    )

    assert summary.succeeded is True
    status = read_status(status_file)
    assert status is not None
    assert status.correction_windows == {0: "uuid-fix"}
    assert status.correction_run == "2026-10-09T12:00:00+00:00"
    assert status.corrected_at == "2026-10-09T12:00:00+00:00"
    assert status.correction_episodes == ["uuid-fix"]


async def test_ingest_auto_runs_diarization_before_live_ingest(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("test-stt-key")
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")
    _stub_transcribe(monkeypatch, [(0, 90000, "A")])
    graphiti = make_graphiti(episode_prefix="uuid-")
    _fake_memory(monkeypatch, graphiti)
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    sidecar = record_path.parent / "diarization.json"
    seen: dict[str, bool] = {}
    real_ingest = ingest_service.ingest_until_stopped

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        seen["sidecar_present"] = sidecar.exists()
        return await real_ingest(*args, **kwargs)

    monkeypatch.setattr(ingest_service, "ingest_until_stopped", guarded)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(_write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    assert seen["sidecar_present"] is True
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["schema"] == "diarization_correction/v1"
    assert [(turn["start_ms"], turn["end_ms"]) for turn in payload["turns"]] == [(0, 90000)]
    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


async def test_ingest_no_correct_skips_diarization_entirely(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("test-stt-key")
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")

    def exploding(chunk: bytes, settings: Any, transport: Any = None) -> list[DiarizedTurn]:
        del chunk
        del transport
        del settings
        raise AssertionError("diarization must not run")

    monkeypatch.setattr("fakeyou.diarization.diarize_service.transcribe_chunk", exploding)
    graphiti = make_graphiti()
    _fake_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(_write_persona(tmp_path), None, None),
        twin_name=None,
        no_correct=True,
        settings=settings,
    )

    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    assert not (record_path.parent / "diarization.json").exists()
    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


async def test_ingest_stt_failure_still_ingests_live_transcript(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("test-stt-key")
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")

    def failing(chunk: bytes, settings: Any, transport: Any = None) -> list[DiarizedTurn]:
        del chunk
        del transport
        del settings
        raise DiarizeError(ErrorCode.DIARIZE_TRANSCRIPTION_FAILED, reason="timeout")

    monkeypatch.setattr("fakeyou.diarization.diarize_service.transcribe_chunk", failing)
    graphiti = make_graphiti()
    _fake_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(_write_persona(tmp_path), None, None),
        twin_name=None,
        settings=settings,
    )

    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]
    faults = [
        row
        for row in _journal_rows(settings, "meeting-9")
        if row.get("event") == "fault_recorded" and row.get("source") == "stt"
    ]
    assert len(faults) == 1
    assert faults[0].get("detail") == "diarize.transcription_failed"
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    status = read_status(status_path(record_path))
    assert status is not None
    assert status.status == "completed"


async def test_ingest_rerun_writes_zero_new_correction_episodes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("test-stt-key")
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")
    _stub_transcribe(monkeypatch, [(0, 90000, "A")])
    graphiti = make_graphiti(episode_prefix="uuid-")
    _fake_memory(monkeypatch, graphiti)
    persona_path = _write_persona(tmp_path)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(persona_path, None, None),
        twin_name=None,
        settings=settings,
    )
    group_id = PersonaProfile.model_validate(PERSONA_DATA).memory_group
    record_path = meeting_record_path(settings.paths.record_dir, SessionId("meeting-9"))
    first = await ingest_corrections(graphiti, record_path, MeetingScope(group_id, "meeting-9"))

    assert first.completed is True
    assert len(first.episodes) == 1
    corrected = [
        episode["name"] for episode in graphiti.added if episode["name"].endswith(":corrected")
    ]
    assert corrected == ["meeting-9:w00000:corrected"]
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(persona_path, None, None),
        twin_name=None,
        no_correct=True,
        settings=settings,
    )
    second = await ingest_corrections(graphiti, record_path, MeetingScope(group_id, "meeting-9"))

    assert second.completed is True
    assert second.episodes == ()
    assert [
        episode["name"] for episode in graphiti.added if episode["name"].endswith(":corrected")
    ] == corrected


async def test_ingest_silently_skips_correction_without_key_or_audio(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")
    _write_record(settings, "meeting-10")

    def exploding(chunk: bytes, settings: Any, transport: Any = None) -> list[DiarizedTurn]:
        del chunk
        del transport
        del settings
        raise AssertionError("diarization must not run")

    monkeypatch.setattr("fakeyou.diarization.diarize_service.transcribe_chunk", exploding)
    first_memory = make_graphiti()
    _fake_memory(monkeypatch, first_memory)
    persona_path = _write_persona(tmp_path)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-9"),
        identity=OperatorIdentity(persona_path, None, None),
        twin_name=None,
        settings=settings,
    )
    settings.stt.api_key = SecretStr("test-stt-key")
    second_memory = make_graphiti()
    _fake_memory(monkeypatch, second_memory)
    await ingest_recorded_meeting(
        meeting_id=SessionId("meeting-10"),
        identity=OperatorIdentity(persona_path, None, None),
        twin_name=None,
        settings=settings,
    )

    assert [episode["name"] for episode in first_memory.added] == ["meeting-9:w00000"]
    assert [episode["name"] for episode in second_memory.added] == ["meeting-10:w00000"]
    for meeting_id in ("meeting-9", "meeting-10"):
        record_path = meeting_record_path(settings.paths.record_dir, SessionId(meeting_id))
        assert not (record_path.parent / "diarization.json").exists()
        faults = [
            row
            for row in _journal_rows(settings, meeting_id)
            if row.get("event") == "fault_recorded"
        ]
        assert faults == []


async def test_ingest_episode_name_error_keeps_hint_on_stderr(
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_record(settings, "meeting-9")
    _fake_memory(monkeypatch, make_graphiti())

    async def failing(*args: Any, **kwargs: Any) -> Any:
        del args
        del kwargs
        raise EpisodeNameError(ErrorCode.INGESTION_EPISODE_NAME_INVALID, meeting_id="meeting-9")

    monkeypatch.setattr(ingest_service, "ingest_until_stopped", failing)

    with pytest.raises(SystemExit) as exit:
        await ingest_recorded_meeting(
            meeting_id=SessionId("meeting-9"),
            identity=OperatorIdentity(_write_persona(tmp_path), None, None),
            twin_name=None,
            no_correct=True,
            settings=settings,
        )

    assert exit.value.code == ExitCode.FAILURE
    assert "--keep-record" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_forget_meeting_files_threads_database_timeouts_to_the_checkpointer(
    settings: Settings, postgres_checkpoints: dict[str, Any]
) -> None:
    from urllib.parse import parse_qsl, urlsplit

    from fakeyou.memory.forget import forget_meeting_files

    settings.database.connect_timeout_seconds = 7.0
    settings.database.statement_timeout_seconds = 9.0
    settings.database.keepalive_idle_seconds = 11.0
    profile = PersonaProfile(
        owner_id=settings.identity.owner_id,
        twin_id=settings.identity.twin_id,
        display_name="Settings Twin",
        role="Project coordinator",
        allowed_tools=frozenset(),
    )

    await forget_meeting_files(settings, profile, SessionId("meeting-forget"), meeting_thread_id)

    (conninfo,) = postgres_checkpoints
    params = dict(parse_qsl(urlsplit(conninfo).query))
    assert params["connect_timeout"] == "7"
    assert "statement_timeout=9000" in params["options"]
    assert params["keepalives_idle"] == "11"


def test_finish_ingest_reports_corrupt_status(tmp_path: Path) -> None:
    status_file = tmp_path / "ingestion.json"
    status_file.write_text("{corrupt", encoding="utf-8")
    journal = RecordingJournal()
    faults = FaultReporter(journal=journal)

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)
    ) as caught:
        finish_ingest(
            status_file,
            IngestOutcome(
                state="completed",
                windows=1,
                resumed_from=0,
                ingested=1,
                facts=1,
                completed=True,
                record_sha256="abc",
                reason=None,
                episode_uuids={0: "uuid-a"},
            ),
            meeting_id="meeting-1",
            group_id="twin_group",
            faults=faults,
        )

    assert caught.value.code is ErrorCode.INGESTION_STATUS_CORRUPT
    assert status_file.read_text(encoding="utf-8") == "{corrupt"
    assert len(journal.events("ingestion_status_rejected")) == 1
