import json
from datetime import UTC, datetime
from types import SimpleNamespace

from fakeyou.faults import FaultReporter, is_recordable_fault
from fakeyou.provider_faults import ProviderFault


def _reporter(**overrides):
    values: dict = {"meeting_id": "meeting-1"}
    values.update(overrides)
    return FaultReporter(**values)


def test_storm_collapses_to_one_kind_with_first_detail() -> None:
    reporter = _reporter()

    for index in range(18):
        reporter.record(
            ProviderFault("stt", "unavailable", status=503, detail=f"flap {index}", retryable=True)
        )

    assert reporter.total() == 18
    report = reporter.report()
    assert report == {
        "faults": {
            "kinds": [{"kind": "unavailable", "count": 18, "status": 503, "detail": "flap 0"}],
            "total": 18,
        }
    }


def test_tie_counts_sort_by_kind_ascending() -> None:
    reporter = _reporter()
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    reporter.record(ProviderFault("tts", "unknown", detail="odd", retryable=False))
    reporter.record(ProviderFault("tts", "unknown", detail="odd", retryable=False))

    report = reporter.report()

    assert report is not None
    assert [entry["kind"] for entry in report["faults"]["kinds"]] == ["timeout", "unknown"]
    assert report["faults"]["total"] == 4


def test_counts_sort_before_kind() -> None:
    reporter = _reporter()
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    for _ in range(3):
        reporter.record(ProviderFault("tts", "unknown", detail="odd", retryable=False))

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
        reporter.record(item)

    assert reporter.total() == 0
    assert reporter.report() is None


def test_empty_report_is_none() -> None:
    assert _reporter().report() is None
    assert _reporter().total() == 0


def test_first_of_kind_logs_once_with_meeting_id() -> None:
    lines: list[str] = []
    frozen = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    reporter = FaultReporter(meeting_id="meeting-7", now=lambda: frozen, log=lines.append)

    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    reporter.record(ProviderFault("stt", "timeout", detail="slow again", retryable=True))
    reporter.record(ProviderFault("tts", "unknown", detail="odd", retryable=False))

    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["meeting_id"] == "meeting-7"
    assert first["kind"] == "timeout"
    assert first["at"] == frozen.isoformat()
    second = json.loads(lines[1])
    assert second["kind"] == "unknown"


def test_duck_typed_fault_is_recordable() -> None:
    reporter = _reporter()

    reporter.record(
        SimpleNamespace(source="stt", kind="custom", status=None, detail="native words")
    )

    assert reporter.total() == 1
    report = reporter.report()
    assert report is not None
    assert report["faults"]["kinds"][0]["kind"] == "custom"


def test_is_recordable_fault_matches_provider_and_ducks() -> None:
    assert is_recordable_fault(ProviderFault("stt", "timeout", retryable=True)) is True
    assert is_recordable_fault(SimpleNamespace(source="stt", kind="x")) is True
    assert is_recordable_fault(SimpleNamespace(kind="x")) is False
    assert is_recordable_fault(Exception("x")) is False
    assert is_recordable_fault(None) is False
    assert is_recordable_fault("timeout") is False


def test_report_shape_matches_session_result_contract() -> None:
    reporter = _reporter()
    reporter.record(ProviderFault("agent", "bad_request", status=400, detail="no", retryable=False))

    report = reporter.report()

    assert set(report or {}) == {"faults"}
    assert set((report or {})["faults"]) == {"kinds", "total"}
    assert set((report or {})["faults"]["kinds"][0]) == {"kind", "count", "status", "detail"}


def test_same_kind_from_different_sources_is_grouped_separately() -> None:
    reporter = _reporter()
    reporter.record(ProviderFault("stt", "unavailable", status=503, detail="stt"))
    reporter.record(ProviderFault("tts", "unavailable", status=503, detail="tts"))

    assert reporter.total() == 2
    assert len(reporter.summaries()) == 2
    assert {summary.source for summary in reporter.summaries()} == {"stt", "tts"}


def test_fault_count_survives_logging_failure() -> None:
    def broken(_: str) -> None:
        raise OSError("logging unavailable")

    reporter = _reporter(log=broken)
    reporter.record(ProviderFault("stt", "timeout", detail="slow", retryable=True))
    reporter.record(ProviderFault("stt", "timeout", detail="still slow", retryable=True))

    assert reporter.total() == 2
    assert reporter.report()["faults"]["total"] == 2
