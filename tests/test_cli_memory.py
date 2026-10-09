import json
import struct
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

import fakeyou.cli.memory as cli_memory
from fakeyou.cli.main import build_parser
from fakeyou.cli.memory import _finish_ingest, ingest_recorded_meeting
from fakeyou.conversation import TranscriptEvent
from fakeyou.diarize_client import DiarizedTurn, DiarizeError
from fakeyou.faults import FaultReporter
from fakeyou.ingestion import ingest_corrections
from fakeyou.ingestion_status import IngestionStatus, read_status, status_path, write_status
from fakeyou.invariants import CAPTURE_SAMPLE_RATE
from fakeyou.journal import Journal
from fakeyou.persona import PersonaProfile
from fakeyou.record import MeetingRecord, meeting_record_path
from fakeyou.settings import Settings
from fakeyou.worker_backend import DockerWorkerBackend
from tests.fakes import make_graphiti
from tests.test_worker_backend import FINISHED_ROWS, FakeRunner, write_journal

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
    record = MeetingRecord(meeting_record_path(settings.paths.record_dir, meeting_id))
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
    audio_dir = meeting_record_path(settings.paths.record_dir, meeting_id).parent / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "slots.json").write_text(
        json.dumps(
            {
                "schema": "slot_audio/v1",
                "slots": {"0": {"seq_start": 0, "seq_end": 10, "frames": 1600, "gaps_filled": 0}},
                "anchors": [],
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
    def fake(chunk: bytes, *, api_key: str, opener: Any = None) -> list[DiarizedTurn]:
        return [
            DiarizedTurn(start_ms=start, end_ms=end, speaker=name) for start, end, name in turns
        ]

    monkeypatch.setattr("fakeyou.cli.diarize.transcribe_chunk", fake)


def _fake_memory(monkeypatch: pytest.MonkeyPatch, memory: Any) -> None:
    @asynccontextmanager
    async def open_memory(_settings: Settings) -> Any:
        yield memory

    monkeypatch.setattr(cli_memory, "open_persona_memory", open_memory)


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
    faults = FaultReporter(journal=Journal("meeting-1", stdout=False))

    ok = _finish_ingest(
        status_file,
        meeting_id="meeting-1",
        group_id="twin_group",
        state="completed",
        windows=3,
        resumed_from=0,
        ingested=3,
        facts=1,
        completed=True,
        record_sha256="abc",
        reason=None,
        faults=faults,
        episode_uuids={1: "uuid-b"},
    )

    assert ok is True
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
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    sidecar = record_path.parent / "diarization.json"
    seen: dict[str, bool] = {}
    real_ingest = cli_memory.ingest_until_stopped

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        seen["sidecar_present"] = sidecar.exists()
        return await real_ingest(*args, **kwargs)

    monkeypatch.setattr(cli_memory, "ingest_until_stopped", guarded)
    await ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=_write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
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

    def exploding(chunk: bytes, *, api_key: str, opener: Any = None) -> list[DiarizedTurn]:
        raise AssertionError("diarization must not run")

    monkeypatch.setattr("fakeyou.cli.diarize.transcribe_chunk", exploding)
    graphiti = make_graphiti()
    _fake_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=_write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
        twin_name=None,
        no_correct=True,
        settings=settings,
    )

    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    assert not (record_path.parent / "diarization.json").exists()
    assert [episode["name"] for episode in graphiti.added] == ["meeting-9:w00000"]


def test_memory_ingest_parser_registers_no_correct() -> None:
    arguments = build_parser().parse_args(
        ["memory-ingest", "--meeting", "m1", "--persona", "p.json", "--no-correct"]
    )

    assert arguments.handler is ingest_recorded_meeting
    assert arguments.no_correct is True
    defaulted = build_parser().parse_args(
        ["memory-ingest", "--meeting", "m1", "--persona", "p.json"]
    )

    assert defaulted.no_correct is False


async def test_ingest_stt_failure_still_ingests_live_transcript(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.api_key = SecretStr("test-stt-key")
    _write_record(settings, "meeting-9")
    _write_audio_slots(settings, "meeting-9")

    def failing(chunk: bytes, *, api_key: str, opener: Any = None) -> list[DiarizedTurn]:
        raise DiarizeError("transcription request failed")

    monkeypatch.setattr("fakeyou.cli.diarize.transcribe_chunk", failing)
    graphiti = make_graphiti()
    _fake_memory(monkeypatch, graphiti)
    await ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=_write_persona(tmp_path),
        owner_id=None,
        twin_id=None,
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
    assert faults[0].get("detail") == "transcription request failed"
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
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
        meeting_id="meeting-9",
        persona_path=persona_path,
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )
    group_id = PersonaProfile.model_validate(PERSONA_DATA).memory_group
    record_path = meeting_record_path(settings.paths.record_dir, "meeting-9")
    first = await ingest_corrections(
        graphiti, record_path, group_id=group_id, meeting_id="meeting-9"
    )

    assert first.completed is True
    assert len(first.episodes) == 1
    corrected = [
        episode["name"] for episode in graphiti.added if episode["name"].endswith(":corrected")
    ]
    assert corrected == ["meeting-9:w00000:corrected"]
    await ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=persona_path,
        owner_id=None,
        twin_id=None,
        twin_name=None,
        no_correct=True,
        settings=settings,
    )
    second = await ingest_corrections(
        graphiti, record_path, group_id=group_id, meeting_id="meeting-9"
    )

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

    def exploding(chunk: bytes, *, api_key: str, opener: Any = None) -> list[DiarizedTurn]:
        raise AssertionError("diarization must not run")

    monkeypatch.setattr("fakeyou.cli.diarize.transcribe_chunk", exploding)
    first_memory = make_graphiti()
    _fake_memory(monkeypatch, first_memory)
    persona_path = _write_persona(tmp_path)
    await ingest_recorded_meeting(
        meeting_id="meeting-9",
        persona_path=persona_path,
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )
    settings.stt.api_key = SecretStr("test-stt-key")
    second_memory = make_graphiti()
    _fake_memory(monkeypatch, second_memory)
    await ingest_recorded_meeting(
        meeting_id="meeting-10",
        persona_path=persona_path,
        owner_id=None,
        twin_id=None,
        twin_name=None,
        settings=settings,
    )

    assert [episode["name"] for episode in first_memory.added] == ["meeting-9:w00000"]
    assert [episode["name"] for episode in second_memory.added] == ["meeting-10:w00000"]
    for meeting_id in ("meeting-9", "meeting-10"):
        record_path = meeting_record_path(settings.paths.record_dir, meeting_id)
        assert not (record_path.parent / "diarization.json").exists()
        faults = [
            row
            for row in _journal_rows(settings, meeting_id)
            if row.get("event") == "fault_recorded"
        ]
        assert faults == []


def test_worker_ingest_routes_through_memory_ingest_without_skip(tmp_path: Path) -> None:
    runner = FakeRunner()
    runner.expect(" run ", "abc123\n")
    backend = DockerWorkerBackend(
        runner=runner,
        journal_dir=tmp_path / "journals",
        record_dir=tmp_path / "meetings",
        project_dir=tmp_path,
    )
    write_journal(tmp_path / "journals" / "meeting-9.jsonl", FINISHED_ROWS)
    record = meeting_record_path(tmp_path / "meetings", "meeting-9")
    path = status_path(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_status(
        path,
        IngestionStatus(meeting_id="meeting-9", group_id="group-1", status="completed"),
    )
    runtime = tmp_path / ".runtime" / "audio"
    runtime.mkdir(parents=True)
    persona = runtime / "persona.json"
    persona.write_text("{}", encoding="utf-8")

    ingested = backend.ingest("meeting-9", persona_path=persona)

    assert ingested.status == "completed"
    run = next(command for command in runner.commands if "run" in command)
    tail = run[run.index("fakeyou") + 1 :]
    assert tail[0] == "memory-ingest"
    assert "--no-correct" not in tail
