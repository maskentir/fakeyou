from datetime import timedelta
from typing import Any

from fakeyou.observability.report import (
    build_session_report,
)
from tests._report_support import START, answered_turn, row


def streamed_turn(offset: float, transcript_id: str) -> list[dict[str, Any]]:
    request_id = f"{transcript_id}:1"
    return [
        row(offset, "speech_stopped", source="audio"),
        row(
            offset + 0.4,
            "transcript_finalized",
            transcript_id=transcript_id,
            text="When is the demo?",
        ),
        row(offset + 0.4, "agent_started", request_id=request_id),
        row(
            offset + 1.0,
            "answer_text_emitted",
            source="llm",
            request_id=request_id,
            transcript_id=transcript_id,
        ),
        row(
            offset + 1.2,
            "answer_sentence_emitted",
            source="agent",
            request_id=request_id,
            transcript_id=transcript_id,
            sentence_index=0,
            chars=20,
        ),
        row(
            offset + 1.4,
            "answer_sentence_emitted",
            source="agent",
            request_id=request_id,
            transcript_id=transcript_id,
            sentence_index=1,
            chars=21,
        ),
        row(offset + 1.6, "agent_finished", request_id=request_id, duration_ms=1200),
        row(offset + 1.6, "playback_started", source="playback", text="The demo is soon."),
        row(
            offset + 1.7,
            "synthesis_started",
            source="tts",
            request_id="s1",
            chars=20,
            streaming=True,
            sentence_index=0,
        ),
        row(
            offset + 2.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=800,
            bytes_out=2,
            sentence_index=0,
        ),
        row(offset + 7.0, "playback_finished", source="playback", ends_with_question=False),
    ]


def test_streamed_turn_reports_first_text_and_first_sentence_stages() -> None:
    report = build_session_report(streamed_turn(0, "t1"))
    [turn] = report["turns"]

    assert turn["streamed"] is True
    assert turn["sentence_count"] == 2
    assert turn["agent_to_first_text_seconds"] == 0.6
    assert turn["agent_to_first_sentence_seconds"] == 0.8
    assert turn["first_sentence_to_first_pcm_seconds"] == 1.3
    assert turn["reply_to_first_pcm_seconds"] == 0.9
    assert report["latency"]["agent_to_first_text_seconds"] == {
        "count": 1,
        "p50": 0.6,
        "p95": 0.6,
        "max": 0.6,
    }
    assert report["latency"]["agent_to_first_sentence_seconds"]["count"] == 1
    assert report["latency"]["first_sentence_to_first_pcm_seconds"]["count"] == 1
    assert report["evidence"] == {"excluded": {}}


def test_unstreamed_turn_leaves_stream_stages_empty_without_new_exclusions() -> None:
    report = build_session_report(answered_turn(0, "t1", "The demo is on Friday."))
    [turn] = report["turns"]

    assert turn["streamed"] is False
    assert turn["sentence_count"] == 0
    assert turn["agent_to_first_text_seconds"] is None
    assert turn["agent_to_first_sentence_seconds"] is None
    assert turn["first_sentence_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {}}
    assert report["latency"]["agent_to_first_text_seconds"] == {
        "count": 0,
        "p50": None,
        "p95": None,
        "max": None,
    }


def test_streamed_turn_without_pcm_excludes_sentence_to_pcm_stage() -> None:
    rows = [
        entry
        for entry in streamed_turn(0, "t1")
        if entry["event"] not in ("synthesis_started", "synthesis_pcm_emitted")
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["streamed"] is True
    assert turn["sentence_count"] == 2
    assert turn["agent_to_first_sentence_seconds"] == 0.8
    assert turn["first_sentence_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {"missing_timestamp": 3}}


def test_negative_stream_latency_is_excluded_with_reasons() -> None:
    rows = streamed_turn(0, "t1")
    early = (START + timedelta(seconds=0.1)).isoformat()
    for entry in rows:
        if entry["event"] in ("answer_text_emitted", "answer_sentence_emitted"):
            entry["at"] = early

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["streamed"] is True
    assert turn["agent_to_first_text_seconds"] is None
    assert turn["agent_to_first_sentence_seconds"] is None
    assert turn["final_to_first_sentence_seconds"] is None
    assert turn["first_sentence_to_first_pcm_seconds"] == 2.4
    assert report["evidence"]["excluded"]["negative_latency"] == 3


def test_report_counts_requests_with_unusable_durations_without_adding_time() -> None:
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-lost",
            transcript_id="t1",
            duration_ms=None,
        ),
        row(
            1.0,
            "tool_failed",
            source="agent",
            request_id="tool-1",
            tool="recall_meeting_context",
            transcript_id="t1",
            duration_ms=float("nan"),
            error="TimeoutError",
        ),
        row(
            1.1,
            "tool_failed",
            source="agent",
            request_id="tool-2",
            tool="recall_meeting_context",
            transcript_id="t1",
            duration_ms=-5,
            error="TimeoutError",
        ),
    ]

    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    [turn] = report["turns"]

    assert (turn["llm_count"], turn["llm_duration_seconds"]) == (1, 0)
    assert (turn["recall_count"], turn["recall_duration_seconds"]) == (2, 0)
    assert report["evidence"] == {"excluded": {"nonfinite_duration": 1, "invalid_duration": 1}}


def test_streamed_synthesis_links_to_own_turn_by_transcript() -> None:
    rows = [
        row(0.0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="tA", text="first"),
        row(0.4, "agent_started", request_id="tA:1"),
        row(1.0, "agent_finished", request_id="tA:1", duration_ms=600),
        row(1.0, "playback_started", source="playback", text="Reply A."),
        row(1.1, "synthesis_started", source="tts", request_id="sA", chars=8, streaming=True),
        row(
            1.9,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="sA",
            first_chunk_ms=800,
            bytes_out=100,
        ),
        row(
            2.0,
            "synthesis_finished",
            source="tts",
            request_id="sA",
            duration_ms=900,
            first_chunk_ms=800,
        ),
        row(3.0, "speech_stopped", source="audio"),
        row(3.4, "transcript_finalized", transcript_id="tB", text="second"),
        row(3.4, "agent_started", request_id="tB:2"),
        row(4.0, "answer_text_emitted", source="llm", request_id="tB:2", transcript_id="tB"),
        row(
            4.2,
            "answer_sentence_emitted",
            source="agent",
            request_id="tB:2",
            transcript_id="tB",
            sentence_index=0,
            chars=20,
        ),
        row(4.2, "playback_started", source="playback", text="Reply B."),
        row(
            4.3,
            "synthesis_started",
            source="tts",
            request_id="sB",
            transcript_id="tB",
            chars=20,
            streaming=True,
            sentence_index=0,
        ),
        row(
            5.3,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="sB",
            transcript_id="tB",
            first_chunk_ms=1000,
            bytes_out=120,
            sentence_index=0,
        ),
        row(
            5.4,
            "synthesis_finished",
            source="tts",
            request_id="sB",
            transcript_id="tB",
            duration_ms=1100,
            first_chunk_ms=1000,
        ),
        row(5.4, "agent_finished", request_id="tB:2", duration_ms=2000),
    ]
    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    first, second = report["turns"]
    assert first["speech_stopped_to_first_pcm_seconds"] == 1.9
    assert second["streamed"] is True
    assert second["sentence_count"] == 1
    assert second["first_sentence_to_first_pcm_seconds"] == 1.1
    assert second["speech_stopped_to_first_pcm_seconds"] == 2.3


def test_final_to_first_sentence_and_routed_strong_are_reported() -> None:
    rows = streamed_turn(0, "t1")
    rows.insert(3, row(0.5, "model_route_selected", source="agent", route="strong", trigger="look"))
    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["final_to_first_sentence_seconds"] == 0.8
    assert turn["routed_strong"] is True
    assert report["latency"]["final_to_first_sentence_seconds"]["p50"] == 0.8


def test_a_small_only_turn_is_not_routed_strong() -> None:
    rows = streamed_turn(0, "t1")
    rows.insert(3, row(0.5, "model_route_selected", source="agent", route="small", trigger="none"))
    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["routed_strong"] is False
