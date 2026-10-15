import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fakeyou.config.settings import IngestionSettings
from fakeyou.diarization.diarize_client import DiarizedTurn
from fakeyou.diarization.diarize_correction import write_correction_bundle
from fakeyou.memory.ingestion import IngestControl, IngestScope, ingest_meeting
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.faults import FaultReporter
from fakeyou.observability.journal import Journal
from fakeyou.persistence.record import TranscriptEntry

START = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)


CORRECTED_AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def said(seconds: float, text: str, participant_id: str = "unknown") -> TranscriptEntry:
    return TranscriptEntry(
        at=START + timedelta(seconds=seconds),
        id=f"t{seconds}",
        spoken_at=(START + timedelta(seconds=seconds)).isoformat(),
        source="meeting_audio",
        text=text,
        participant_id=participant_id,
        speaker_evidence_id=None,
    )


async def run(memory: MagicMock, entries: list[Any], **overrides: Any) -> tuple[Any, list[Any]]:
    lines: list[str] = []
    journal = overrides.pop("journal", Journal("meeting-1", emit=lines.append))
    faults = overrides.pop("faults", FaultReporter(journal=journal))
    scope = IngestScope(
        overrides.pop("group_id", "twin_group"),
        overrides.pop("meeting_id", "meeting-1"),
        overrides.pop("twin_name", "Rani Twin"),
        overrides.pop("vocabulary", ("Orchid", "Budi")),
    )
    ingestion_updates = {
        key: overrides.pop(key)
        for key in ("window_utterances", "window_seconds", "episode_timeout_seconds")
        if key in overrides
    }
    control = IngestControl(
        IngestionSettings().model_copy(update=ingestion_updates),
        overrides.pop("stop", None),
    )
    assert not overrides, overrides
    result = await ingest_meeting(
        memory, entries, scope, control, Diagnostics(faults=faults, journal=journal)
    )
    return result, [json.loads(line) for line in lines]


def correction_record(tmp_path: Path, speaker: str = "Harun") -> Path:
    lines = [
        (
            0,
            "The budget is approved for the Thursday launch, including the rollout checklist, "
            "the client demo schedule, and the release notes for all regions.",
            1000.0,
        ),
        (
            61,
            "Budi is the PIC for the Thursday launch and owns the rollout checklist "
            "end to end without further review from the client team.",
            70000.0,
        ),
        (
            122,
            "We ship on Friday after the client demo, and the changelog plus the release notes "
            "will be ready on time for all regions this week.",
            130000.0,
        ),
    ]
    entries = [
        TranscriptEntry(
            at=START + timedelta(seconds=seconds),
            id=f"c{index}",
            spoken_at=(START + timedelta(seconds=seconds)).isoformat(),
            source="meeting_audio",
            text=text,
            participant_id="unknown",
            speaker_evidence_id=None,
            capture_ms=capture,
        )
        for index, (seconds, text, capture) in enumerate(lines)
    ]
    record_path = tmp_path / "meeting-1" / "record.jsonl"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        "".join(entry.model_dump_json() + "\n" for entry in entries), encoding="utf-8"
    )
    write_correction_bundle(
        record_path.parent / "audio",
        record_path,
        [DiarizedTurn(start_ms=69000, end_ms=71000, speaker=speaker)],
        clock=lambda: CORRECTED_AT,
    )
    return record_path
