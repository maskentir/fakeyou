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
        row(offset, "speech_stopped", source="capture"),
        row(
            offset + 0.4, "transcript_final", transcript_id=transcript_id, text="When is the demo?"
        ),
        row(offset + 0.4, "agent_started", request_id=f"{transcript_id}:1"),
    ]
    if recall:
        rows.append(
            row(offset + 1.0, "tool_finished", tool="recall_meeting_context", duration_ms=500)
        )
    rows += [
        row(offset + 1.6, "agent_finished", request_id=f"{transcript_id}:1", latency_ms=1200),
        row(offset + 1.6, "speak_started", source="playback", text=reply),
        row(offset + 4.6, "synth_finished", duration_ms=3000, first_chunk_latency_ms=900),
        row(
            offset + 7.0,
            "speak_finished",
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
    assert first["speech_stopped_to_final_s"] == 0.4
    assert first["final_to_reply_s"] == 1.2
    assert first["reply_to_first_pcm_s"] == 0.9
    assert first["speech_stopped_to_first_pcm_s"] == 2.5
    assert first["outcome"] == "spoken"
    assert report["turns"][1]["tools"] == ["recall_meeting_context"]
    assert report["latency"]["speech_stopped_to_first_pcm_s"] == {
        "count": 2,
        "p50": 2.5,
        "p95": 2.5,
        "max": 2.5,
    }
    assert report["replies"] == {"spoken": 2, "interrupted": 0, "trailing_questions": 1}


def test_report_marks_interrupted_and_cancelled_turns() -> None:
    rows = [
        row(0, "transcript_final", transcript_id="t1", text="Explain it"),
        row(0, "agent_started", request_id="t1:1"),
        row(1, "agent_finished", request_id="t1:1", latency_ms=1000),
        row(1, "speak_started", source="playback", text="A very long reply."),
        row(3, "speech_started", source="capture"),
        row(4, "speech_stopped", source="capture"),
        row(4.5, "transcript_final", transcript_id="t2", text="Okay enough"),
        row(
            4.5, "speak_dropped", source="playback", text="A very long reply.", detail="interrupted"
        ),
        row(4.5, "agent_started", request_id="t2:2"),
        row(4.6, "agent_cancelled", request_id="t2:2", latency_ms=100),
    ]

    report = build_session_report(rows)

    assert [turn["outcome"] for turn in report["turns"]] == ["interrupted", "cancelled"]
    assert report["turns"][1]["speech_stopped_to_final_s"] == 0.5
    assert report["replies"]["interrupted"] == 1


def test_report_summarizes_ingestion_faults_tools_and_result() -> None:
    rows = [
        row(
            1,
            "memory_episode",
            speaker_kind="participant",
            facts=2,
            outcome="extracted",
            duration_ms=9000,
        ),
        row(2, "memory_episode", facts=0, outcome="empty", duration_ms=3000),
        row(3, "memory_episode", facts=0, outcome="failed", duration_ms=100),
        row(4, "fault", source="memory_ingest", kind="timeout"),
        row(5, "fault", source="memory_ingest", kind="timeout"),
        row(6, "tool_finished", tool="recall_meeting_context", duration_ms=800, output_lines=3),
        row(
            7, "tool_failed", tool="recall_meeting_context", duration_ms=15000, error="TimeoutError"
        ),
        row(8, "session_result", phase="ended", terminal_reason="owner_stop", exit_code=0),
    ]

    report = build_session_report(rows)

    assert report["ingestion"] == {
        "episodes": 3,
        "facts": 2,
        "empty": 1,
        "failed": 1,
        "duration_s": {"count": 3, "p50": 3.0, "p95": 8.4, "max": 9.0},
    }
    assert report["faults"] == [{"source": "memory_ingest", "kind": "timeout", "count": 2}]
    assert report["tools"] == {"recall_meeting_context": {"finished": 1, "failed": 1}}
    assert report["result"] == {"phase": "ended", "terminal_reason": "owner_stop", "exit_code": 0}


def test_report_measures_startup_from_admission() -> None:
    rows = [
        row(0, "audio_ready", source="runtime", duration_ms=2400),
        row(0.1, "join_requested", source="runtime"),
        row(
            30.1,
            "join_result",
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
        "audio_startup_s": 2.4,
        "join_status": "admitted",
        "lobby_wait_s": 30.0,
        "audio_ready_at_admission": True,
        "admission_to_microphone_s": 2.4,
        "admission_to_listening_s": 2.5,
    }


def test_report_of_a_denied_join_has_no_admission_timings() -> None:
    rows = [
        row(0, "audio_ready", source="runtime", duration_ms=2000),
        row(
            5, "join_result", source="runtime", status="denied", duration_ms=5000, audio_ready=True
        ),
    ]

    startup = build_session_report(rows)["startup"]

    assert (startup["join_status"], startup["lobby_wait_s"]) == ("denied", 5.0)
    assert startup["audio_ready_at_admission"] is None
    assert startup["admission_to_microphone_s"] is None


def test_report_of_empty_journal_is_explicit() -> None:
    report = build_session_report([])

    assert report["startup"]["join_status"] is None
    assert report["turns"] == []
    assert report["latency"]["final_to_reply_s"] == {
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
        row(0, "transcript_final", transcript_id="t1", text="Explain it"),
        row(0, "agent_finished", request_id="t1:1", latency_ms=500),
        row(0.5, "speak_started", source="playback", text="Long reply."),
        row(2.0, "speech_started", source="capture"),
        row(2.9, "barge_in", trigger="interim", words=3),
        row(3.0, "speak_dropped", source="playback", text="Long reply.", detail="interrupted"),
        row(10, "speak_started", source="playback", text="Another reply."),
        row(11, "speech_started", source="capture"),
        row(12.5, "barge_in", trigger="final", words=2),
        row(12.5, "speak_dropped", source="playback", text="Another reply.", detail="interrupted"),
    ]

    interruptions = build_session_report(rows)["interruptions"]

    assert interruptions["triggers"] == {"interim": 1, "final": 1}
    assert interruptions["speech_to_stop_s"] == {"count": 2, "p50": 1.25, "p95": 1.475, "max": 1.5}


def test_report_ignores_speech_that_started_before_the_interrupted_playback() -> None:
    rows = [
        row(0, "speech_started", source="capture"),
        row(1, "speak_started", source="playback", text="A reply."),
        row(2, "speak_dropped", source="playback", text="A reply.", detail="interrupted"),
    ]

    assert build_session_report(rows)["interruptions"]["speech_to_stop_s"]["count"] == 0


def test_report_should_count_trailing_questions_without_retaining_reply_text():
    report = build_session_report(
        [
            row(1, "speak_finished", ends_with_question=True),
            row(2, "speak_finished", ends_with_question=False),
            row(3, "speak_finished"),
            row(4, "speak_dropped", ends_with_question=True),
        ]
    )

    assert report["replies"] == {"spoken": 3, "interrupted": 1, "trailing_questions": 1}
