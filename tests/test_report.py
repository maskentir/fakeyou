import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeyou.cli.diagnostics as cli_diagnostics
from fakeyou.report import build_session_report, read_journal

START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)


def row(seconds: float, event: str, **fields) -> dict:
    return {
        "event": event,
        "meeting_id": "m1",
        "at": (START + timedelta(seconds=seconds)).isoformat(),
        **fields,
    }


def answered_turn(
    offset: float,
    transcript_id: str,
    reply: str,
    *,
    recall: bool = False,
    participant_id: str = "unknown",
    speaker_evidence_id: str | None = None,
) -> list:
    rows = [
        row(offset, "speech_stopped", source="audio"),
        row(
            offset + 0.4,
            "transcript_finalized",
            transcript_id=transcript_id,
            text="When is the demo?",
            participant_id=participant_id,
            speaker_evidence_id=speaker_evidence_id,
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


def streamed_turn(offset: float, transcript_id: str) -> list:
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
    assert turn["first_sentence_to_first_pcm_seconds"] == 2.4
    assert report["evidence"]["excluded"]["negative_latency"] == 2


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


def test_streamed_synthesis_links_to_own_turn_by_transcript():
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


def test_diagnostics_report_surfaces_both_outcomes(tmp_path: Path, settings, capsys) -> None:
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

    cli_diagnostics.show_session_report("m9", tmp_path, settings)

    printed = json.loads(capsys.readouterr().out)
    assert printed["meeting_id"] == "m9"
    assert printed["session_outcome"] == "completed"
    assert printed["ingestion_outcome"] == "completed"
    assert printed["cancellation"] == {"requested": 1, "completed": 0}


def test_report_summarizes_participation_and_policy() -> None:
    rows = (
        answered_turn(
            0,
            "t1",
            "The demo is on Friday.",
            participant_id="spaces/x/devices/1",
            speaker_evidence_id="e1",
        )
        + [
            row(
                1.2,
                "request_finished",
                source="llm",
                request_id="t1:1",
                transcript_id="t1",
                duration_ms=800,
            ),
        ]
        + [
            row(1.0, "policy_decided", transcript_id="t1", action="reply", reason="addressed"),
            row(
                2.0,
                "transcript_finalized",
                transcript_id="t2",
                text="Budi, when is the demo?",
                participant_id="unknown",
                speaker_evidence_id="e9",
            ),
            row(2.0, "policy_decided", transcript_id="t2", action="silent", reason="not_addressed"),
            row(
                3.0,
                "transcript_finalized",
                transcript_id="t3",
                text="I agree with Budi.",
                participant_id="unknown",
                speaker_evidence_id=None,
            ),
            row(3.0, "policy_decided", transcript_id="t3", action="silent", reason="not_addressed"),
            row(0.5, "participant_observer_started", source="runtime", method="dom_mining"),
        ]
    )

    report = build_session_report(rows)

    assert report["participation"]["utterances_recorded"] == 3
    assert report["participation"]["attributed_utterances"] == 1
    assert report["participation"]["attribution_coverage"] == 0.333
    assert report["participation"]["unknown_reasons"] == {
        "evidence_unmatched": 1,
        "no_evidence": 1,
    }
    assert report["participation"]["answers"] == 1
    assert report["participation"]["policy"] == {"reply": 1, "silent": 2}
    assert report["participation"]["policy_reasons"] == {
        "addressed": 1,
        "not_addressed": 2,
    }
    assert report["participation"]["observer_methods"] == {"dom_mining": 1}


def test_report_counts_memory_prefetch_as_recall_for_its_transcript():
    rows = [
        row(0, "speech_stopped", source="audio"),
        row(0.4, "transcript_finalized", transcript_id="t1", text="First"),
        row(0.4, "agent_started", request_id="t1:1"),
        row(
            0.5,
            "request_started",
            source="memory",
            request_id="m1",
            operation="prefetch",
            transcript_id="t1",
        ),
        row(
            0.8,
            "request_finished",
            source="memory",
            request_id="m1",
            operation="prefetch",
            transcript_id="t1",
            outcome="found",
            duration_ms=300,
        ),
        row(1.6, "agent_finished", request_id="t1:1", duration_ms=1200),
    ]

    turn = build_session_report(rows)["turns"][0]

    assert (turn["recall_count"], turn["recall_duration_seconds"]) == (1, 0.3)
    assert turn["llm_count"] == 0


def test_report_summarizes_stt_quality_signals() -> None:
    rows = [
        row(0, "order_inversion_detected", source="stt", transcript_id="t2"),
        row(1, "order_inversion_detected", source="stt", transcript_id="t3"),
        row(2, "stt_turn_recorded", source="stt", interims=2, first_interim_to_final_ms=120.0),
        row(3, "stt_turn_recorded", source="stt", interims=0, first_interim_to_final_ms=None),
        row(
            4,
            "capture_polled",
            source="browser",
            slots={"0": {"engine": "decode"}, "1": {"engine": "mstp"}},
        ),
    ]

    quality = build_session_report(rows)["stt_quality"]

    assert quality["order_inversions"] == 2
    assert quality["turn_interims"] == {"count": 2, "p50": 1.0, "p95": 1.9, "max": 2}
    assert quality["first_interim_to_final_ms"] == {
        "count": 1,
        "p50": 120.0,
        "p95": 120.0,
        "max": 120.0,
    }
    assert quality["capture_engines"] == {"decode": 1, "mstp": 1}


def test_session_report_counts_drain_gaps() -> None:
    rows = [
        {"event": "drain_gap_detected", "slot": 0, "kind": "gap", "missed": 2},
        {"event": "drain_gap_detected", "slot": 1, "kind": "resync", "missed": 0},
    ]
    assert build_session_report(rows)["stt_quality"]["drain_gaps"] == 2


def test_session_report_counts_tap_slot_rescues() -> None:
    rows = [
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 1},
        {"event": "tap_slot_rescued", "slot": 0, "rescued": 2},
    ]
    assert build_session_report(rows)["stt_quality"]["tap_slot_rescues"] == 2


def test_session_report_counts_tap_slot_drops() -> None:
    rows = [
        {"event": "tap_slot_dropped", "slot": 0},
        {"event": "tap_slot_dropped", "slot": 2},
    ]
    assert build_session_report(rows)["stt_quality"]["tap_slot_drops"] == 2


def test_session_report_summarizes_pregeneration() -> None:
    rows = [
        {"event": "pregeneration_started", "request_id": "a", "words": 4},
        {
            "event": "pregeneration_finished",
            "request_id": "a",
            "outcome": "discarded",
            "reason": "superseded",
            "dist": 1,
            "words_pre": 4,
            "words_final": 5,
            "norm_dist_bucket": 0.2,
            "diff_class": "prefix_extend",
        },
        {"event": "pregeneration_started", "request_id": "b", "words": 5},
        {
            "event": "pregeneration_finished",
            "request_id": "b",
            "outcome": "adopted",
            "duration_ms": 3000.0,
            "dist": 0,
            "words_pre": 5,
            "words_final": 5,
            "norm_dist_bucket": 0.0,
            "diff_class": "exact",
        },
    ]
    section = build_session_report(rows)["pregeneration"]
    assert section["starts"] == 2
    assert section["outcomes"] == {"discarded": 1, "adopted": 1}
    assert section["reasons"] == {"superseded": 1}
    assert section["diff_classes"] == {"prefix_extend": 1, "exact": 1}
    assert section["norm_dist_buckets"] == {"0.2": 1, "0.0": 1}
    assert section["adopted_overlap_seconds"] == {
        "count": 1,
        "p50": 3.0,
        "p95": 3.0,
        "max": 3.0,
    }


def meet_requested(offset: float, action: str, action_id: str, **fields) -> dict:
    return row(
        offset,
        "meet_action_requested",
        source="agent",
        action=action,
        action_id=action_id,
        actor="twin",
        attempt=1,
        **fields,
    )


def test_meet_actions_correlate_lifecycle_by_action_id() -> None:
    rows = [
        meet_requested(0, "raise_hand", "a1"),
        row(1, "meet_action_started", source="agent", action="raise_hand", action_id="a1"),
        row(
            2,
            "meet_action_observed",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed_state="hand_raised",
        ),
        row(
            3,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed=True,
            attempts=1,
            duration_ms=3000,
        ),
        meet_requested(4, "lower_hand", "a2"),
        row(
            5,
            "meet_action_started",
            source="agent",
            action="lower_hand",
            action_id="a2",
            attempt=2,
        ),
        row(
            6,
            "meet_action_failed",
            source="agent",
            action="lower_hand",
            action_id="a2",
            reason="timeout",
            attempts=2,
            duration_ms=1500,
        ),
    ]

    section = build_session_report(rows)["meet_actions"]

    assert section["actions"] == [
        {
            "action_id": "a1",
            "action": "raise_hand",
            "outcome": "finished",
            "reason": None,
            "observed": True,
            "attempts": 1,
            "duration_ms": 3000,
        },
        {
            "action_id": "a2",
            "action": "lower_hand",
            "outcome": "failed",
            "reason": "timeout",
            "observed": False,
            "attempts": 2,
            "duration_ms": 1500,
        },
    ]


def test_meet_action_finish_without_proof_is_excluded_as_unobserved() -> None:
    rows = [
        meet_requested(0, "raise_hand", "a1"),
        row(
            1,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed=True,
            duration_ms=1000,
        ),
        meet_requested(2, "raise_hand", "a2"),
        row(
            3,
            "meet_action_observed",
            source="agent",
            action="raise_hand",
            action_id="a2",
            observed_state="hand_raised",
        ),
        row(
            4,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a2",
            observed=True,
            duration_ms=1000,
        ),
        meet_requested(5, "lower_hand", "a3"),
        row(
            6,
            "meet_action_finished",
            source="agent",
            action="lower_hand",
            action_id="a3",
            observed=False,
            duration_ms=1000,
        ),
    ]

    report = build_session_report(rows)

    assert report["evidence"]["excluded"] == {"unobserved_finish": 1}
    assert [entry["observed"] for entry in report["meet_actions"]["actions"]] == [
        False,
        True,
        False,
    ]


def test_meet_action_rejections_are_terminal_without_retry() -> None:
    rows: list = []
    for index, reason in enumerate(["authority_denied", "not_admitted", "duplicate_suppressed"]):
        base = index * 10
        rows += [
            meet_requested(base, "send_chat", f"r{index}"),
            row(
                base + 1,
                "meet_action_rejected",
                source="agent",
                action="send_chat",
                action_id=f"r{index}",
                reason=reason,
            ),
            row(
                base + 2,
                "meet_action_started",
                source="agent",
                action="send_chat",
                action_id=f"r{index}",
                attempt=2,
            ),
        ]

    report = build_session_report(rows)
    section = report["meet_actions"]

    assert section["rejections"] == {
        "authority_denied": 1,
        "not_admitted": 1,
        "duplicate_suppressed": 1,
    }
    assert [entry["outcome"] for entry in section["actions"]] == ["rejected"] * 3
    assert [entry["attempts"] for entry in section["actions"]] == [1, 1, 1]
    assert report["evidence"]["excluded"] == {"duplicate": 3}


def test_meet_actions_report_counts_rejections_and_durations() -> None:
    rows = [
        meet_requested(0, "raise_hand", "a1"),
        row(
            1,
            "meet_action_observed",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed_state="hand_raised",
        ),
        row(
            2,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed=True,
            duration_ms=2000,
        ),
        meet_requested(3, "raise_hand", "a2"),
        row(
            4,
            "meet_action_failed",
            source="agent",
            action="raise_hand",
            action_id="a2",
            reason="timeout",
            duration_ms=1000,
        ),
        meet_requested(5, "send_chat", "c1", chars=12),
        row(
            6,
            "meet_action_rejected",
            source="agent",
            action="send_chat",
            action_id="c1",
            reason="authority_denied",
        ),
        meet_requested(7, "lower_hand", "p1"),
        meet_requested(8, "lower_hand", "bad"),
        row(
            9,
            "meet_action_failed",
            source="agent",
            action="lower_hand",
            action_id="bad",
            reason="timeout",
            duration_ms=-5,
        ),
    ]

    report = build_session_report(rows)
    section = report["meet_actions"]

    assert section["counts"] == {
        "raise_hand": {"finished": 1, "failed": 1},
        "send_chat": {"rejected": 1},
        "lower_hand": {"pending": 1, "failed": 1},
    }
    assert section["rejections"] == {"authority_denied": 1}
    assert section["duration_seconds"] == {"count": 2, "p50": 1.5, "p95": 1.95, "max": 2.0}
    for entry in section["actions"]:
        assert set(entry) == {
            "action_id",
            "action",
            "outcome",
            "reason",
            "observed",
            "attempts",
            "duration_ms",
        }
    assert report["evidence"]["excluded"] == {"invalid_duration": 1}
    assert build_session_report([])["meet_actions"] == {
        "counts": {},
        "rejections": {},
        "duration_seconds": {"count": 0, "p50": None, "p95": None, "max": None},
        "actions": [],
    }


def test_meet_actions_first_terminal_wins_and_missing_ids_excluded() -> None:
    rows = [
        meet_requested(0, "raise_hand", "a1"),
        row(
            1,
            "meet_action_observed",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed_state="hand_raised",
        ),
        row(
            2,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed=True,
            duration_ms=1000,
        ),
        row(
            3,
            "meet_action_failed",
            source="agent",
            action="raise_hand",
            action_id="a1",
            reason="timeout",
            duration_ms=2000,
        ),
        row(4, "meet_action_requested", source="agent", action="raise_hand"),
        row(5, "meet_action_finished", source="agent", action="raise_hand", observed=True),
    ]

    report = build_session_report(rows)

    assert report["meet_actions"]["actions"] == [
        {
            "action_id": "a1",
            "action": "raise_hand",
            "outcome": "finished",
            "reason": None,
            "observed": True,
            "attempts": 1,
            "duration_ms": 1000,
        }
    ]
    assert report["evidence"]["excluded"] == {"duplicate": 1, "missing_action_id": 2}
