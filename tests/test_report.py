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
        row(offset + 0.4, "transcript_final", transcript_id=transcript_id, text="Kapan demo?"),
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
        row(offset + 7.0, "speak_finished", source="playback", text=reply),
    ]
    return rows


def test_report_breaks_down_latency_per_turn() -> None:
    rows = answered_turn(0, "t1", "Demonya Jumat.") + answered_turn(
        10, "t2", "Ada yang lain?", recall=True
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
        row(0, "transcript_final", transcript_id="t1", text="Jelasin"),
        row(0, "agent_started", request_id="t1:1"),
        row(1, "agent_finished", request_id="t1:1", latency_ms=1000),
        row(1, "speak_started", source="playback", text="Panjang sekali."),
        row(3, "speech_started", source="capture"),
        row(4, "speech_stopped", source="capture"),
        row(4.5, "transcript_final", transcript_id="t2", text="Oke cukup"),
        row(4.5, "speak_dropped", source="playback", text="Panjang sekali.", detail="interrupted"),
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


def test_report_of_empty_journal_is_explicit() -> None:
    report = build_session_report([])

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
