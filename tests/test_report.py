import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fakeyou.report import build_session_report, read_journal

START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def row(seconds: float, event: str, **fields) -> dict:
    return {
        "event": event,
        "meeting_id": "m1",
        "at": (START + timedelta(seconds=seconds)).isoformat(),
        **fields,
    }


def answered_turn(offset: float, transcript_id: str, reply: str, *, recall: bool = False) -> list:
    rows = [
        row(offset, "speech_stopped", source="audio"),
        row(
            offset + 0.4,
            "transcript_finalized",
            transcript_id=transcript_id,
            text="When is the demo?",
        ),
        row(offset + 0.4, "agent_started", request_id=f"{transcript_id}:1"),
    ]
    if recall:
        rows.append(
            row(offset + 1.0, "tool_finished", tool="recall_meeting_context", duration_ms=500)
        )
    rows += [
        row(offset + 1.6, "agent_finished", request_id=f"{transcript_id}:1", duration_ms=1200),
        row(offset + 1.6, "playback_started", source="playback", text=reply),
        row(offset + 4.6, "synthesis_finished", duration_ms=3000, first_chunk_ms=900),
        row(
            offset + 7.0,
            "playback_finished",
            source="playback",
            ends_with_question=reply.rstrip().endswith("?"),
        ),
    ]
    return rows


def test_report_breaks_down_latency_per_turn() -> None:
    rows = answered_turn(0, "t1", "The demo is on Friday.") + answered_turn(
        10, "t2", "Anything else?", recall=True
    )

    report = build_session_report(rows)

    first = report["turns"][0]
    assert first["transcript_id"] == "t1"
    assert first["speech_stopped_to_final_seconds"] == 0.4
    assert first["final_to_reply_seconds"] == 1.2
    assert first["reply_to_first_pcm_seconds"] == 0.9
    assert first["speech_stopped_to_first_pcm_seconds"] == 2.5
    assert first["outcome"] == "spoken"
    assert report["turns"][1]["tools"] == ["recall_meeting_context"]
    assert report["latency"]["speech_stopped_to_first_pcm_seconds"] == {
        "count": 2,
        "p50": 2.5,
        "p95": 2.5,
        "max": 2.5,
    }
    assert report["replies"] == {"spoken": 2, "interrupted": 0, "trailing_questions": 1}


def test_report_marks_interrupted_and_cancelled_turns() -> None:
    rows = [
        row(0, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0, "agent_started", request_id="t1:1"),
        row(1, "agent_finished", request_id="t1:1", duration_ms=1000),
        row(1, "playback_started", source="playback", text="A very long reply."),
        row(3, "speech_started", source="audio"),
        row(4, "speech_stopped", source="audio"),
        row(4.5, "transcript_finalized", transcript_id="t2", text="Okay enough"),
        row(
            4.5,
            "playback_dropped",
            source="playback",
            text="A very long reply.",
            detail="interrupted",
        ),
        row(4.5, "agent_started", request_id="t2:2"),
        row(4.6, "agent_cancelled", request_id="t2:2", duration_ms=100),
    ]

    report = build_session_report(rows)

    assert [turn["outcome"] for turn in report["turns"]] == ["interrupted", "cancelled"]
    assert report["turns"][1]["speech_stopped_to_final_seconds"] == 0.5
    assert report["replies"]["interrupted"] == 1


def test_report_summarizes_ingestion_faults_tools_and_result() -> None:
    rows = [
        row(1, "memory_episode_ingested", facts=2, duration_ms=9000),
        row(2, "memory_episode_ingested", facts=0, duration_ms=3000),
        row(3, "memory_episode_failed", facts=0, duration_ms=100),
        row(4, "fault_recorded", source="memory_ingest", kind="timeout"),
        row(5, "fault_recorded", source="memory_ingest", kind="timeout"),
        row(6, "tool_finished", tool="recall_meeting_context", duration_ms=800, output_lines=3),
        row(
            7, "tool_failed", tool="recall_meeting_context", duration_ms=15000, error="TimeoutError"
        ),
        row(8, "session_finished", phase="ended", terminal_reason="stopped_by_owner", exit_code=0),
    ]

    report = build_session_report(rows)

    assert report["ingestion"] == {
        "episodes": 3,
        "facts": 2,
        "empty": 1,
        "failed": 1,
        "duration_seconds": {"count": 3, "p50": 3.0, "p95": 8.4, "max": 9.0},
    }
    assert report["faults"] == [{"source": "memory_ingest", "kind": "timeout", "count": 2}]
    assert report["tools"] == {"recall_meeting_context": {"finished": 1, "failed": 1}}
    assert report["result"] == {
        "phase": "ended",
        "terminal_reason": "stopped_by_owner",
        "exit_code": 0,
    }


def test_report_measures_startup_from_admission() -> None:
    rows = [
        row(0, "audio_opened", source="runtime", duration_ms=2400),
        row(0.1, "join_requested", source="runtime"),
        row(
            30.1,
            "join_finished",
            source="runtime",
            status="admitted",
            duration_ms=30000,
            audio_ready=True,
        ),
        row(32.5, "microphone_enabled", source="runtime"),
        row(32.6, "conversation_bound", source="runtime"),
        row(40, "microphone_enabled", source="runtime"),
    ]

    assert build_session_report(rows)["startup"] == {
        "audio_startup_seconds": 2.4,
        "join_status": "admitted",
        "lobby_wait_seconds": 30.0,
        "audio_ready_at_admission": True,
        "admission_to_microphone_seconds": 2.4,
        "admission_to_listening_seconds": 2.5,
    }


def test_report_of_a_denied_join_has_no_admission_timings() -> None:
    rows = [
        row(0, "audio_opened", source="runtime", duration_ms=2000),
        row(
            5,
            "join_finished",
            source="runtime",
            status="denied",
            duration_ms=5000,
            audio_ready=True,
        ),
    ]

    startup = build_session_report(rows)["startup"]

    assert (startup["join_status"], startup["lobby_wait_seconds"]) == ("denied", 5.0)
    assert startup["audio_ready_at_admission"] is None
    assert startup["admission_to_microphone_seconds"] is None


def test_report_of_empty_journal_is_explicit() -> None:
    report = build_session_report([])

    assert report["startup"]["join_status"] is None
    assert report["turns"] == []
    assert report["latency"]["final_to_reply_seconds"] == {
        "count": 0,
        "p50": None,
        "p95": None,
        "max": None,
    }
    assert report["result"] is None


def test_read_journal_merges_rotated_files_oldest_first(tmp_path: Path) -> None:
    base = tmp_path / "m1.jsonl"
    for suffix, event in (("", "third"), (".1", "second"), (".2", "first")):
        Path(f"{base}{suffix}").write_text(json.dumps(row(0, event)) + "\n", encoding="utf-8")
    Path(f"{base}.1").write_text(
        Path(f"{base}.1").read_text(encoding="utf-8") + "not json\n", encoding="utf-8"
    )

    assert [entry["event"] for entry in read_journal(base)] == ["first", "second", "third"]


def test_report_measures_interruption_latency_from_speech_start() -> None:
    rows = [
        row(0, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0, "agent_finished", request_id="t1:1", duration_ms=500),
        row(0.5, "playback_started", source="playback", text="Long reply."),
        row(2.0, "speech_started", source="audio"),
        row(2.9, "barge_in_detected", trigger="interim", words=3),
        row(3.0, "playback_dropped", source="playback", text="Long reply.", detail="interrupted"),
        row(10, "playback_started", source="playback", text="Another reply."),
        row(11, "speech_started", source="audio"),
        row(12.5, "barge_in_detected", trigger="final", words=2),
        row(
            12.5, "playback_dropped", source="playback", text="Another reply.", detail="interrupted"
        ),
    ]

    interruptions = build_session_report(rows)["interruptions"]

    assert interruptions["triggers"] == {"interim": 1, "final": 1}
    assert interruptions["speech_to_stop_seconds"] == {
        "count": 2,
        "p50": 1.25,
        "p95": 1.475,
        "max": 1.5,
    }


def test_report_ignores_speech_that_started_before_the_interrupted_playback() -> None:
    rows = [
        row(0, "speech_started", source="audio"),
        row(1, "playback_started", source="playback", text="A reply."),
        row(2, "playback_dropped", source="playback", text="A reply.", detail="interrupted"),
    ]

    assert build_session_report(rows)["interruptions"]["speech_to_stop_seconds"]["count"] == 0


def test_report_should_count_trailing_questions_without_retaining_reply_text():
    report = build_session_report(
        [
            row(1, "playback_finished", ends_with_question=True),
            row(2, "playback_finished", ends_with_question=False),
            row(3, "playback_finished"),
            row(4, "playback_dropped", ends_with_question=True),
        ]
    )

    assert report["replies"] == {"spoken": 3, "interrupted": 1, "trailing_questions": 1}


def test_interrupted_stream_with_pcm_still_reports_first_pcm_latency():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(
            1.7,
            "synthesis_started",
            source="tts",
            request_id="s1",
            chars=6,
            streaming=True,
        ),
        row(
            2.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=800,
            bytes_out=2,
        ),
        row(2.6, "speech_started", source="audio"),
        row(
            3.0,
            "playback_dropped",
            source="playback",
            detail="interrupted",
            ends_with_question=False,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["outcome"] == "interrupted"
    assert turn["reply_to_first_pcm_seconds"] == 0.9
    assert turn["speech_stopped_to_first_pcm_seconds"] == 2.5
    assert report["evidence"] == {"excluded": {}}


def test_late_synthesis_finish_uses_its_request_mapping():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:1"),
        row(11.6, "agent_finished", request_id="t2:1", duration_ms=1200),
        row(
            12.0,
            "synthesis_finished",
            source="tts",
            request_id="s1",
            duration_ms=3000,
            first_chunk_ms=900,
            bytes_out=4,
            streaming=True,
        ),
    ]

    report = build_session_report(rows)
    first, second = report["turns"]

    assert first["reply_to_first_pcm_seconds"] == 8.3
    assert first["speech_stopped_to_first_pcm_seconds"] == 9.9
    assert second["reply_to_first_pcm_seconds"] is None
    assert second["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {"missing_timestamp": 2}}


def test_synthesis_finished_without_chunk_proof_yields_no_pcm_latency():
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows[5] = row(4.6, "synthesis_finished", duration_ms=3000, first_chunk_ms=None)

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["final_to_reply_seconds"] == 1.2
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None


def test_clean_turn_reports_no_excluded_evidence():
    report = build_session_report(answered_turn(0, "t1", "The demo is on Friday."))

    assert report["evidence"] == {"excluded": {}}
    assert build_session_report([])["evidence"] == {"excluded": {}}


def test_missing_timestamps_are_excluded_with_reasons():
    report = build_session_report(
        [row(0.4, "transcript_finalized", transcript_id="t1", text="Hello")]
    )
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] is None
    assert turn["final_to_agent_seconds"] is None
    assert turn["agent_to_reply_seconds"] is None
    assert turn["final_to_reply_seconds"] is None
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"] == {"excluded": {"missing_timestamp": 6}}


def test_negative_latency_is_excluded_with_reasons():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            1.0,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=100,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] == 0.4
    assert turn["final_to_reply_seconds"] == 1.2
    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] == 1.0
    assert report["evidence"] == {"excluded": {"negative_latency": 1}}


def test_nonfinite_durations_are_excluded_with_reasons():
    rows = []
    for offset, transcript_id, first_chunk_ms, duration_ms in [
        (0, "t1", float("inf"), 3000),
        (10, "t2", 900, float("nan")),
    ]:
        rows += [
            row(offset, "speech_stopped", source="audio"),
            row(offset + 0.4, "transcript_finalized", transcript_id=transcript_id, text="Hi"),
            row(offset + 0.4, "agent_started", request_id=f"{transcript_id}:1"),
            row(offset + 1.6, "agent_finished", request_id=f"{transcript_id}:1"),
            row(offset + 1.6, "playback_started", source="playback", ends_with_question=False),
            row(offset + 1.7, "synthesis_started", source="tts", request_id=f"s-{transcript_id}"),
            row(
                offset + 4.6,
                "synthesis_finished",
                source="tts",
                request_id=f"s-{transcript_id}",
                duration_ms=duration_ms,
                first_chunk_ms=first_chunk_ms,
            ),
        ]

    report = build_session_report(rows)

    assert [turn["reply_to_first_pcm_seconds"] for turn in report["turns"]] == [None, None]
    assert [turn["final_to_reply_seconds"] for turn in report["turns"]] == [1.2, 1.2]
    assert report["evidence"] == {"excluded": {"nonfinite_duration": 2, "missing_timestamp": 4}}


def test_invalid_durations_are_excluded_with_reasons():
    cases = [(-1, 3000), (5000, 3000), ("soon", 3000), (900, -5), (True, 3000)]
    for index, (first_chunk_ms, duration_ms) in enumerate(cases):
        transcript_id = f"t{index}"
        rows = [
            row(0, "speech_stopped", source="audio"),
            row(0.4, "transcript_finalized", transcript_id=transcript_id, text="Hi"),
            row(0.4, "agent_started", request_id=f"{transcript_id}:1"),
            row(1.6, "agent_finished", request_id=f"{transcript_id}:1"),
            row(1.6, "playback_started", source="playback", ends_with_question=False),
            row(1.7, "synthesis_started", source="tts", request_id="s1"),
            row(
                4.6,
                "synthesis_finished",
                source="tts",
                request_id="s1",
                duration_ms=duration_ms,
                first_chunk_ms=first_chunk_ms,
            ),
        ]

        report = build_session_report(rows)
        [turn] = report["turns"]

        assert turn["reply_to_first_pcm_seconds"] is None
        assert turn["speech_stopped_to_first_pcm_seconds"] is None
        assert report["evidence"] == {"excluded": {"invalid_duration": 1, "missing_timestamp": 2}}


def test_malformed_timestamps_do_not_break_the_report():
    rows = [
        {"event": "speech_stopped", "meeting_id": "m1", "at": "not-a-time", "source": "audio"},
        row(0.4, "transcript_finalized", transcript_id="t1", text="Hi"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1"),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["speech_stopped_to_final_seconds"] is None
    assert turn["final_to_reply_seconds"] == 1.2
    assert report["evidence"]["excluded"]["missing_timestamp"] == 3


def test_synthesis_events_without_a_recorded_start_are_excluded():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(
            2.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="lost",
            first_chunk_ms=800,
            bytes_out=2,
        ),
        row(
            4.6,
            "synthesis_finished",
            source="tts",
            request_id="lost",
            duration_ms=3000,
            first_chunk_ms=900,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["reply_to_first_pcm_seconds"] is None
    assert turn["speech_stopped_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["missing_synthesis_start"] == 2


def test_synthesis_start_without_an_answering_turn_is_ambiguous():
    rows = [
        row(0.4, "transcript_finalized", transcript_id="t1", text="Explain it"),
        row(0.5, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            1.5,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=800,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    [turn] = report["turns"]

    assert turn["reply_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["ambiguous_synthesis_request"] == 1


def test_duplicate_synthesis_start_marks_the_request_ambiguous():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
        row(1.6, "playback_started", source="playback", ends_with_question=False),
        row(1.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(10, "speech_stopped", source="audio"),
        row(10.4, "transcript_finalized", transcript_id="t2", text="Second"),
        row(10.4, "agent_started", request_id="t2:1"),
        row(11.6, "agent_finished", request_id="t2:1", duration_ms=1200),
        row(11.7, "synthesis_started", source="tts", request_id="s1", streaming=True),
        row(
            12.0,
            "synthesis_pcm_emitted",
            source="tts",
            request_id="s1",
            first_chunk_ms=300,
            bytes_out=2,
        ),
    ]

    report = build_session_report(rows)
    first, second = report["turns"]

    assert first["reply_to_first_pcm_seconds"] is None
    assert second["reply_to_first_pcm_seconds"] is None
    assert report["evidence"]["excluded"]["ambiguous_synthesis_request"] == 1


def test_report_correlates_model_time_to_its_transcript():
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    rows += [
        row(0.5, "request_started", source="llm", request_id="llm-1", transcript_id="t1"),
        row(
            1.5,
            "request_finished",
            source="llm",
            request_id="llm-1",
            transcript_id="t1",
            duration_ms=1000,
        ),
    ]
    report = build_session_report(sorted(rows, key=lambda entry: entry["at"]))
    turn = report["turns"][0]
    assert turn["llm_count"] == 1
    assert turn["llm_duration_seconds"] == 1.0
    assert turn["final_to_agent_seconds"] == 0.0
    assert turn["agent_to_reply_seconds"] == 1.2


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


def test_report_counts_requests_with_unusable_durations_without_adding_time():
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
