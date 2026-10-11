import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from fakeyou.observability.faults import Fault, FaultReporter, record_consumer_fault
from fakeyou.observability.journal import Journal, JournalFile
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.observability.report import build_session_report, read_journal


def _reporter(**overrides: Any) -> FaultReporter:
    values: dict[str, Any] = {"meeting_id": "meeting-1"}
    values.update(overrides)
    return FaultReporter(
        journal=Journal(values["meeting_id"], now=values.get("now"), emit=values.get("log"))
    )


def test_storm_collapses_to_one_kind_with_first_detail() -> None:
    reporter = _reporter()

    for index in range(18):
        reporter.record(
            ProviderFault(
                "stt", "unavailable", evidence=FaultEvidence(503, f"flap {index}"), retryable=True
            )
        )

    assert reporter.total() == 18
    report = reporter.report()
    assert report == {
        "faults": {
            "kinds": [
                {
                    "source": "stt",
                    "operation": None,
                    "kind": "unavailable",
                    "count": 18,
                    "status": 503,
                    "detail": "flap 0",
                }
            ],
            "total": 18,
        }
    }


def test_tie_counts_sort_by_kind_ascending() -> None:
    reporter = _reporter()
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("tts", "unknown", evidence=FaultEvidence(detail="odd"), retryable=False)
    )
    reporter.record(
        ProviderFault("tts", "unknown", evidence=FaultEvidence(detail="odd"), retryable=False)
    )

    report = reporter.report()

    assert report is not None
    assert [entry["kind"] for entry in report["faults"]["kinds"]] == ["timeout", "unknown"]
    assert report["faults"]["total"] == 4


def test_counts_sort_before_kind() -> None:
    reporter = _reporter()
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    for _ in range(3):
        reporter.record(
            ProviderFault("tts", "unknown", evidence=FaultEvidence(detail="odd"), retryable=False)
        )

    report = reporter.report()

    assert report is not None
    assert [entry["kind"] for entry in report["faults"]["kinds"]] == ["unknown", "timeout"]


def test_record_never_raises_and_ignores_garbage() -> None:
    reporter = _reporter()
    garbage = [
        None,
        Exception("x"),
        "timeout",
        42,
        SimpleNamespace(kind="timeout"),
        SimpleNamespace(source="stt"),
        SimpleNamespace(),
    ]

    for item in garbage:
        reporter.record(cast(Any, item))

    assert reporter.total() == 0
    assert reporter.report() is None


def test_empty_report_is_none() -> None:
    assert _reporter().report() is None
    assert _reporter().total() == 0


def test_first_of_kind_logs_once_with_meeting_id() -> None:
    lines: list[str] = []
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    reporter = FaultReporter(journal=Journal("meeting-7", now=lambda: frozen, emit=lines.append))

    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow again"), retryable=True)
    )
    reporter.record(
        ProviderFault("tts", "unknown", evidence=FaultEvidence(detail="odd"), retryable=False)
    )

    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["meeting_id"] == "meeting-7"
    assert first["kind"] == "timeout"
    assert first["at"] == frozen.isoformat()
    second = json.loads(lines[1])
    assert second["kind"] == "unknown"


def test_health_fault_is_recorded_with_its_operation() -> None:
    reporter = _reporter()

    reporter.record(Fault(source="stt", kind="no_transcripts", detail="silent provider"))
    reporter.record(
        ProviderFault(
            "browser",
            "timeout",
            operation="camera",
            evidence=FaultEvidence(detail="slow"),
            retryable=True,
        )
    )

    assert reporter.total() == 2
    report = reporter.report()
    assert report is not None
    assert [(e["source"], e["operation"], e["kind"]) for e in report["faults"]["kinds"]] == [
        ("browser", "camera", "timeout"),
        ("stt", None, "no_transcripts"),
    ]


def test_report_shape_matches_session_result_contract() -> None:
    reporter = _reporter()
    reporter.record(
        ProviderFault("agent", "bad_request", evidence=FaultEvidence(400, "no"), retryable=False)
    )

    report = reporter.report()

    assert set(report or {}) == {"faults"}
    assert set((report or {})["faults"]) == {"kinds", "total"}
    assert set((report or {})["faults"]["kinds"][0]) == {
        "source",
        "operation",
        "kind",
        "count",
        "status",
        "detail",
    }


def test_same_kind_from_different_sources_is_grouped_separately() -> None:
    reporter = _reporter()
    reporter.record(ProviderFault("stt", "unavailable", evidence=FaultEvidence(503, "stt")))
    reporter.record(ProviderFault("tts", "unavailable", evidence=FaultEvidence(503, "tts")))

    assert reporter.total() == 2
    assert len(reporter.summaries()) == 2
    assert {summary.source for summary in reporter.summaries()} == {"stt", "tts"}


def test_fault_count_survives_logging_failure() -> None:
    def broken(_: str) -> None:
        raise OSError("logging unavailable")

    reporter = _reporter(log=broken)
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="still slow"), retryable=True)
    )

    assert reporter.total() == 2
    report = reporter.report()
    assert report is not None
    assert report["faults"]["total"] == 2


def test_fault_should_reach_journal_file_and_report_with_shared_identity_and_clock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "journal.jsonl"
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    journal = Journal("meeting-7", file=JournalFile(path), echo=False, now=lambda: frozen)
    reporter = FaultReporter(journal=journal)

    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="slow"), retryable=True)
    )
    reporter.record(
        ProviderFault("stt", "timeout", evidence=FaultEvidence(detail="again"), retryable=True)
    )
    journal.close()

    rows = read_journal(path)
    assert len(rows) == 1
    assert rows[0]["meeting_id"] == "meeting-7"
    assert rows[0]["at"] == frozen.isoformat()
    assert reporter.summaries()[0].first_at == rows[0]["at"]
    assert reporter.total() == 2
    assert build_session_report(rows)["faults"] == [
        {"source": "stt", "kind": "timeout", "count": 1}
    ]


def test_consumer_fault_should_share_journal_file_identity_and_clock(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    journal = Journal("meeting-7", file=JournalFile(path), echo=False, now=lambda: frozen)
    reporter = FaultReporter(journal=journal)
    record_consumer_fault(
        reporter, None, source="stt", detail="transcript_consumer", error=ValueError("private")
    )
    journal.close()

    rows = read_journal(path)
    assert [(row["event"], row["kind"]) for row in rows] == [("fault_recorded", "callback_failed")]
    assert all(row["at"] == frozen.isoformat() and row["meeting_id"] == "meeting-7" for row in rows)
    assert "private" not in path.read_text()
