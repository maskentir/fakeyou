from fakeyou.observability.report import (
    build_session_report,
)
from tests._report_support import answered_turn, row


def test_report_attributes_parallel_and_stale_tool_results_by_transcript():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(
            0.5,
            "tool_started",
            source="agent",
            request_id="tool-1",
            tool="recall_meeting_context",
            transcript_id="t1",
        ),
        row(
            0.6,
            "tool_started",
            source="agent",
            request_id="tool-2",
            tool="recall_meeting_context",
            transcript_id="t1",
        ),
        row(
            1.0,
            "tool_finished",
            source="agent",
            request_id="tool-1",
            tool="recall_meeting_context",
            transcript_id="t1",
            duration_ms=500,
            output_lines=2,
        ),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:2"),
        row(
            10.5,
            "tool_finished",
            source="agent",
            request_id="tool-2",
            tool="recall_meeting_context",
            transcript_id="t1",
            duration_ms=9900,
            output_lines=1,
        ),
        row(11.6, "agent_finished", request_id="t2:2", duration_ms=1200),
    ]

    report = build_session_report(rows)
    first, second = report["turns"]

    assert (first["recall_count"], first["recall_duration_seconds"]) == (2, 10.4)
    assert first["tools"] == ["recall_meeting_context", "recall_meeting_context"]
    assert first["incomplete_request_count"] == 0
    assert (second["recall_count"], second["recall_duration_seconds"]) == (0, 0)
    assert second["tools"] == []
    assert second["incomplete_request_count"] == 0


def test_report_excludes_provider_events_with_unknown_or_missing_transcript():
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(0.5, "request_started", source="llm", request_id="llm-x", transcript_id="ghost"),
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-x",
            transcript_id="ghost",
            duration_ms=1000,
        ),
        row(
            1.0,
            "tool_finished",
            source="agent",
            request_id="tool-x",
            tool="recall_meeting_context",
            duration_ms=500,
        ),
    ]

    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    [turn] = report["turns"]

    assert turn["llm_count"] == 0
    assert turn["recall_count"] == 0
    assert turn["incomplete_request_count"] == 0
    assert turn["tools"] == ["recall_meeting_context"]
    assert report["evidence"] == {"excluded": {"unknown_transcript": 1, "missing_transcript": 1}}


def test_report_counts_unfinished_requests_as_incomplete():
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-lost",
            transcript_id="t1",
            duration_ms=800,
        ),
        row(0.6, "request_started", source="llm", request_id="llm-hung", transcript_id="t1"),
        row(
            0.7,
            "tool_started",
            source="agent",
            request_id="tool-hung",
            tool="recall_meeting_context",
            transcript_id="t1",
        ),
    ]

    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    [turn] = report["turns"]

    assert (turn["llm_count"], turn["llm_duration_seconds"]) == (1, 0.8)
    assert turn["recall_count"] == 0
    assert turn["incomplete_request_count"] == 2


def test_report_keeps_provider_time_on_failed_and_cancelled_turns():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(0.5, "request_started", source="llm", request_id="llm-1", transcript_id="t1"),
        row(
            1.5,
            "request_failed",
            source="llm",
            request_id="llm-1",
            transcript_id="t1",
            duration_ms=1000,
            error="TimeoutError",
        ),
        row(1.6, "agent_failed", request_id="t1:1", duration_ms=1200),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:2"),
        row(10.5, "request_started", source="llm", request_id="llm-2", transcript_id="t2"),
        row(10.6, "agent_cancelled", request_id="t2:2", duration_ms=200),
    ]

    report = build_session_report(rows)
    failed, cancelled = report["turns"]

    assert failed["outcome"] == "failed"
    assert (failed["llm_count"], failed["llm_duration_seconds"]) == (1, 1.0)
    assert failed["incomplete_request_count"] == 0
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["llm_count"] == 0
    assert cancelled["incomplete_request_count"] == 1


def test_report_separates_queue_wait_from_agent_work():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.0, "speech_stopped", source="audio"),
        row(1.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "agent_started", request_id="t2:2"),
        row(2.8, "agent_finished", request_id="t2:2", duration_ms=1200),
    ]

    report = build_session_report(rows)
    first, queued = report["turns"]

    assert first["final_to_agent_seconds"] == 0.0
    assert queued["final_to_agent_seconds"] == 0.2
    assert queued["agent_to_reply_seconds"] == 1.2
    assert queued["final_to_reply_seconds"] == 1.4
