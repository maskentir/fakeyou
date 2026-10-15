import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from fakeyou.observability.faults import (
    TRACEBACK_MAX_CHARS,
    ConsumerSink,
    Fault,
    FaultReporter,
    bounded_traceback,
    capture_error,
    create_poll_guard,
    guard_consumer,
    guard_observer,
    guard_poll,
    record_consumer_fault,
)
from fakeyou.observability.journal import Journal, JournalFile
from fakeyou.observability.provider_faults import FaultEvidence, ProviderFault
from fakeyou.observability.report import build_session_report, read_journal
from tests._persistence_fakes import RecordingJournal


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
        ConsumerSink(faults=reporter),
        source="stt",
        detail="transcript_consumer",
        error=ValueError("private"),
    )
    journal.close()

    rows = read_journal(path)
    assert [(row["event"], row["kind"]) for row in rows] == [("fault_recorded", "callback_failed")]
    assert all(row["at"] == frozen.isoformat() and row["meeting_id"] == "meeting-7" for row in rows)
    assert "private" not in path.read_text()


async def test_guard_consumer_runs_callback() -> None:

    calls: list[int] = []

    async def call() -> None:
        calls.append(1)

    await guard_consumer(call, sink=ConsumerSink(), source="stt", detail="probe")

    assert calls == [1]


async def test_guard_consumer_records_consumer_error_as_fault() -> None:

    async def boom() -> None:
        raise RuntimeError("consumer gone")

    reporter = _reporter()
    await guard_consumer(
        boom,
        sink=ConsumerSink(faults=reporter),
        source="stt",
        detail="transcript_consumer",
    )

    (summary,) = reporter.summaries()
    assert summary.kind == "callback_failed"
    assert summary.detail == "transcript_consumer: RuntimeError"


async def test_guard_consumer_bounds_hanging_callback() -> None:

    async def hang() -> None:
        await asyncio.sleep(60.0)

    reporter = _reporter()
    await guard_consumer(
        hang,
        sink=ConsumerSink(faults=reporter),
        source="stt",
        detail="transcript_consumer",
        timeout_seconds=0.01,
    )

    (summary,) = reporter.summaries()
    assert summary.kind == "callback_failed"
    assert summary.detail == "transcript_consumer: TimeoutError"


async def test_guard_consumer_falls_back_to_journal_without_reporter() -> None:

    async def boom() -> None:
        raise OSError("consumer gone")

    lines: list[str] = []
    journal = Journal("meeting-1", emit=lines.append)
    await guard_consumer(
        boom, sink=ConsumerSink(journal=journal), source="audio", detail="vad_consumer"
    )

    rows = [json.loads(line) for line in lines]
    assert [(row["event"], row["kind"]) for row in rows] == [("fault_recorded", "callback_failed")]


async def test_guard_consumer_reraises_cancel() -> None:

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guard_consumer(cancelled, sink=ConsumerSink(), source="stt", detail="probe")


def test_guard_observer_runs_callback() -> None:

    calls: list[int] = []
    guard_observer(lambda: calls.append(1), sink=ConsumerSink(), source="agent", detail="probe")

    assert calls == [1]


def test_guard_observer_records_observer_error_as_fault() -> None:

    def boom() -> None:
        raise ValueError("observer gone")

    reporter = _reporter()
    guard_observer(boom, sink=ConsumerSink(faults=reporter), source="agent", detail="observer")

    (summary,) = reporter.summaries()
    assert summary.kind == "callback_failed"
    assert summary.detail == "observer: ValueError"


def test_guard_observer_ignores_missing_sink() -> None:

    def boom() -> None:
        raise ValueError("observer gone")

    guard_observer(boom, sink=ConsumerSink(), source="agent", detail="observer")


async def test_guard_poll_fallback_keeps_operation(journal: RecordingJournal) -> None:
    async def boom() -> None:
        raise ValueError("poll gone")

    error = await guard_poll(
        boom, sink=ConsumerSink(journal=journal), source="stt", operation="watch"
    )

    assert isinstance(error, ValueError)
    [event] = journal.events("fault_recorded")
    assert event["operation"] == "watch"


async def test_guard_poll_records_classified_fault_and_returns_error(
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)

    async def boom() -> None:
        raise KeyError("poll bug")

    error = await guard_poll(
        boom, sink=ConsumerSink(reporter, journal), source="browser", operation="voice_activity"
    )

    assert isinstance(error, KeyError)
    assert reporter.total() == 1
    [summary] = reporter.summaries()
    assert summary.source == "browser"
    assert summary.operation == "voice_activity"


async def test_guard_poll_falls_back_to_journal_without_reporter(
    journal: RecordingJournal,
) -> None:

    async def boom() -> None:
        raise KeyError("poll bug")

    error = await guard_poll(
        boom, sink=ConsumerSink(None, journal), source="browser", operation="voice_activity"
    )

    assert isinstance(error, KeyError)
    [event] = journal.events("fault_recorded")
    assert event["detail"] == "voice_activity: KeyError"


async def test_guard_poll_reraises_cancelled(journal: RecordingJournal) -> None:

    reporter = FaultReporter(journal=journal)

    async def boom() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guard_poll(
            boom,
            sink=ConsumerSink(reporter, journal),
            source="browser",
            operation="voice_activity",
        )
    assert reporter.total() == 0


def test_create_poll_guard_returns_none_without_sink() -> None:

    assert create_poll_guard(None, source="browser", operation="voice_activity") is None


async def test_create_poll_guard_records_through_reporter(
    journal: RecordingJournal,
) -> None:

    reporter = FaultReporter(journal=journal)
    guard = create_poll_guard(
        ConsumerSink(reporter, journal), source="audio", operation="voice_activity"
    )
    assert guard is not None

    async def boom() -> None:
        raise KeyError("poll bug")

    error = await guard(boom)

    assert isinstance(error, KeyError)
    assert reporter.total() == 1
    [summary] = reporter.summaries()
    assert summary.source == "audio"
    assert summary.operation == "voice_activity"


async def test_capture_error_returns_result_and_no_error() -> None:

    async def call() -> str:
        return "episode-uuid"

    result, error = await capture_error(call)

    assert result == "episode-uuid"
    assert error is None


async def test_capture_error_returns_unexpected_error() -> None:

    async def call() -> str:
        raise ValueError("bug")

    result, error = await capture_error(call)

    assert result is None
    assert isinstance(error, ValueError)


async def test_capture_error_reraises_cancelled() -> None:

    async def call() -> str:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await capture_error(call)


async def test_capture_error_captures_fakeyou_error_for_caller_dispatch() -> None:
    from fakeyou.catalog.errors import ErrorCode, FakeYouError

    async def call() -> str:
        raise FakeYouError(ErrorCode.OPERATION_INTERRUPTED)

    result, error = await capture_error(call)

    assert result is None
    assert isinstance(error, FakeYouError)


def test_bounded_traceback_keeps_only_innermost_frames() -> None:
    from typing import NoReturn

    def level(index: int) -> NoReturn:
        if index <= 0:
            raise RuntimeError("deep bug")
        level(index - 1)

    def outer() -> NoReturn:
        level(20)

    try:
        outer()
    except RuntimeError as error:
        rendered = bounded_traceback(error)

    assert rendered.startswith("RuntimeError: deep bug")
    assert "outer" not in rendered


def test_bounded_traceback_caps_characters() -> None:

    try:
        raise RuntimeError("x" * 5000)
    except RuntimeError as error:
        rendered = bounded_traceback(error)

    assert len(rendered) <= TRACEBACK_MAX_CHARS


def test_bounded_traceback_renders_unraised_errors() -> None:

    rendered = bounded_traceback(RuntimeError("fresh"))

    assert "RuntimeError: fresh" in rendered


async def test_create_poll_guard_falls_back_to_journal_without_reporter(
    journal: RecordingJournal,
) -> None:

    guard = create_poll_guard(
        ConsumerSink(None, journal), source="browser", operation="participant_observer"
    )
    assert guard is not None

    async def boom() -> None:
        raise KeyError("poll bug")

    error = await guard(boom)

    assert isinstance(error, KeyError)
    [event] = journal.events("fault_recorded")
    assert event["detail"] == "participant_observer: KeyError"


def test_a_fault_the_reporter_cannot_record_is_traced_in_the_journal(
    journal: RecordingJournal,
) -> None:
    reporter = FaultReporter(journal=journal)

    reporter.record(cast(Any, object()))

    [event] = journal.events("fault_report_failed")
    assert event["operation"] == "record"
    assert event["error_type"] == "AttributeError"
