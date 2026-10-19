import json
from pathlib import Path

from fakeyou.observability.report import build_session_report, read_journal_with_corrupt
from tests._report_support import row


def test_report_splits_session_and_ingestion_outcomes() -> None:
    rows = [
        row(
            1,
            "session_finished",
            source="cli",
            phase="ended",
            terminal_reason="left_by_owner",
            exit_code=0,
        ),
        row(
            2,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=0,
            ingested=2,
            facts=5,
            completed=True,
        ),
    ]

    report = build_session_report(rows)

    assert report["session_outcome"] == "completed"
    assert report["ingestion_outcome"] == "completed"


def test_report_marks_missing_outcomes_unknown() -> None:
    report = build_session_report([row(1, "speech_started", source="audio")])

    assert report["session_outcome"] == "unknown"
    assert report["ingestion_outcome"] == "unknown"


def test_report_marks_failed_session_and_interrupted_ingest() -> None:
    rows = [
        row(
            1,
            "session_finished",
            source="cli",
            phase="ended",
            terminal_reason="browser_failure",
            exit_code=1,
        ),
        row(
            2,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=1,
            ingested=0,
            facts=0,
            completed=False,
        ),
    ]

    report = build_session_report(rows)

    assert report["session_outcome"] == "failed"
    assert report["ingestion_outcome"] == "interrupted"


def test_report_marks_ingest_failed_on_memory_fault() -> None:
    rows = [
        row(2, "fault_recorded", source="memory_ingest", kind="provider"),
        row(
            3,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=0,
            ingested=0,
            facts=0,
            completed=False,
        ),
    ]

    assert build_session_report(rows)["ingestion_outcome"] == "failed"


def test_report_marks_post_leave_ingest_unknown() -> None:
    rows = [
        row(
            1,
            "session_finished",
            source="cli",
            phase="ended",
            terminal_reason="left_by_owner",
            exit_code=0,
        ),
    ]

    report = build_session_report(rows)

    assert report["session_outcome"] == "completed"
    assert report["ingestion_outcome"] == "unknown"


def test_report_uses_latest_ingest_run_after_resume() -> None:
    rows = [
        row(
            2,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=0,
            ingested=1,
            facts=3,
            completed=False,
        ),
        row(
            9,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=1,
            ingested=1,
            facts=2,
            completed=True,
        ),
    ]

    assert build_session_report(rows)["ingestion_outcome"] == "completed"


def test_report_does_not_carry_fault_into_later_run() -> None:
    rows = [
        row(2, "fault_recorded", source="memory_ingest", kind="provider"),
        row(
            3,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=0,
            ingested=0,
            facts=0,
            completed=False,
        ),
        row(
            9,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=2,
            resumed_from=0,
            ingested=0,
            facts=0,
            completed=False,
        ),
    ]

    assert build_session_report(rows)["ingestion_outcome"] == "interrupted"


def test_report_marks_missing_outcome_keys_unknown() -> None:
    session_rows = [
        row(
            1,
            "session_finished",
            source="cli",
            phase="ended",
            terminal_reason="left_by_owner",
        ),
    ]
    ingest_rows = [
        row(
            2,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=1,
            resumed_from=0,
            ingested=1,
            facts=2,
        ),
    ]

    assert build_session_report(session_rows)["session_outcome"] == "unknown"
    assert build_session_report(ingest_rows)["ingestion_outcome"] == "unknown"


def test_report_counts_cancel_requested_and_completed() -> None:
    rows = [
        row(0, "transcript_finalized", transcript_id="t1"),
        row(0.1, "agent_cancel_requested", request_id="t1:1"),
        row(0.2, "agent_cancel_requested", request_id="t1:2"),
        row(0.3, "agent_cancelled", request_id="t1:1"),
    ]

    assert build_session_report(rows)["cancellation"] == {
        "requested": 2,
        "completed": 1,
    }


def test_journal_report_surfaces_both_outcomes(tmp_path: Path) -> None:
    rows = [
        row(
            1,
            "session_finished",
            source="cli",
            phase="ended",
            terminal_reason="left_by_owner",
            exit_code=0,
        ),
        row(
            2,
            "memory_ingest_finished",
            source="memory_ingest",
            windows=1,
            resumed_from=0,
            ingested=1,
            facts=2,
            completed=True,
        ),
        row(0.1, "agent_cancel_requested", request_id="t1:1"),
    ]
    (tmp_path / "m9.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in rows) + "\n", encoding="utf-8"
    )

    rows, corrupt = read_journal_with_corrupt(tmp_path / "m9.jsonl")
    printed = {"meeting_id": "m9", **build_session_report(rows, corrupt_lines=corrupt)}
    assert printed["meeting_id"] == "m9"
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "completed"
    assert printed["cancellation"] == {"requested": 1, "completed": 0}
