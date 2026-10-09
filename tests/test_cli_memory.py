from pathlib import Path

from fakeyou.cli.memory import _finish_ingest
from fakeyou.faults import FaultReporter
from fakeyou.ingestion_status import IngestionStatus, read_status, write_status
from fakeyou.journal import Journal


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
