import json
from pathlib import Path

import pytest

from fakeyou.catalog.errors import ErrorCode, FakeYouError
from fakeyou.memory.ingestion import IngestScope, MeetingScope, ingest_corrections, ingest_meeting
from fakeyou.observability.diagnostics import Diagnostics
from fakeyou.observability.journal import Journal
from fakeyou.persistence.ingestion_status import IngestionStatus, status_path
from fakeyou.persistence.record import load_meeting_record
from tests._error_support import message_pattern
from tests._ingestion_support import CORRECTED_AT, correction_record
from tests._memory_fakes import make_graphiti


def test_ingestion_status_old_schema_json_defaults_new_correction_fields() -> None:
    payload = {
        "meeting_id": "meeting-1",
        "group_id": "twin_group",
        "status": "completed",
        "windows": 3,
        "resumed_from": 0,
        "ingested": 3,
        "record_sha256": None,
        "updated_at": CORRECTED_AT.isoformat(),
        "reason": None,
        "episode_uuids": {"0": "uuid-a"},
        "corrected_at": CORRECTED_AT.isoformat(),
        "correction_episodes": ["uuid-fix"],
    }

    status = IngestionStatus.model_validate_json(json.dumps(payload))

    assert status.correction_windows == {}
    assert status.correction_run is None


async def test_ingest_corrections_refuses_a_corrupt_status_and_journals_it(tmp_path: Path) -> None:
    record_path = correction_record(tmp_path)
    memory = make_graphiti(episode_prefix="uuid-", edges=[])
    await ingest_meeting(
        memory,
        load_meeting_record(record_path),
        IngestScope(group_id="twin_group", meeting_id="meeting-1", twin_name="Rani Twin"),
    )
    status_file = status_path(record_path)
    status_file.parent.mkdir(parents=True, exist_ok=True)
    status_file.write_text("{corrupt", encoding="utf-8")
    lines: list[str] = []

    with pytest.raises(
        FakeYouError, match=message_pattern(ErrorCode.INGESTION_STATUS_CORRUPT)
    ) as caught:
        await ingest_corrections(
            memory,
            record_path,
            MeetingScope("twin_group", "meeting-1"),
            diagnostics=Diagnostics(journal=Journal("meeting-1", emit=lines.append)),
        )

    assert caught.value.code is ErrorCode.INGESTION_STATUS_CORRUPT
    assert status_file.read_text(encoding="utf-8") == "{corrupt"
    rows = [json.loads(line) for line in lines]
    assert [row["event"] for row in rows].count("ingestion_status_rejected") == 1
