import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fakeyou.replay.compare import compare_journals, compare_metrics

RUNTIME = Path(".runtime/audio")


def row(event: str, seconds: float, **fields: Any) -> dict[str, Any]:
    at = datetime(2026, 10, 27, 15, 0, tzinfo=datetime.now().astimezone().tzinfo)
    return {
        "event": event,
        "meeting_id": "m-1",
        "at": (at + timedelta(seconds=seconds)).isoformat(),
        **fields,
    }


def session() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [row("speech_started", 0.0), row("speech_stopped", 1.0)]
    for index in range(5):
        final = 1.2 + index
        rows.append(row("transcript_finalized", final, transcript_id=f"t{index}"))
        rows.append(
            row(
                "synthesis_pcm_emitted",
                final + 1.0,
                transcript_id=f"t{index}",
                first_chunk_ms=1000.0,
            )
        )
    return rows


def test_the_same_journal_compares_as_equal() -> None:
    rows = session()

    comparison = compare_journals(rows, rows)

    assert comparison.passed
    assert comparison.failures == ()


def test_a_latency_that_moves_by_half_fails_the_gate() -> None:
    original = session()
    replay = [
        row(
            "synthesis_pcm_emitted",
            3.2,
            transcript_id=entry["transcript_id"],
            first_chunk_ms=2000.0,
        )
        if entry["event"] == "synthesis_pcm_emitted"
        else dict(entry)
        for entry in original
    ]

    comparison = compare_journals(original, replay)

    assert not comparison.passed
    failed = {check.name for check in comparison.failures}
    assert "final_to_first_pcm_ms.p50" in failed


def test_a_latency_that_moves_inside_the_tolerance_passes() -> None:
    original = session()
    replay = [
        row("synthesis_pcm_emitted", 2.3, transcript_id="t0", first_chunk_ms=1100.0)
        if entry["event"] == "synthesis_pcm_emitted" and entry["transcript_id"] == "t0"
        else dict(entry)
        for entry in original
    ]

    comparison = compare_journals(original, replay)

    assert comparison.passed


def test_the_p95_is_reported_but_not_gated() -> None:
    original = session()
    replay = [
        row("synthesis_pcm_emitted", 9.0, transcript_id="t4", first_chunk_ms=6000.0)
        if entry["event"] == "synthesis_pcm_emitted" and entry["transcript_id"] == "t4"
        else dict(entry)
        for entry in original
    ]

    comparison = compare_journals(original, replay)

    p95 = next(check for check in comparison.checks if check.name == "final_to_first_pcm_ms.p95")
    assert not p95.gated
    assert comparison.passed


def test_a_count_built_from_synthesized_interims_is_reported_not_gated() -> None:
    comparison = compare_metrics(
        {"barge_in_rejected": 20, "early_turn_started": 2},
        {"barge_in_rejected": 40, "early_turn_started": 0},
    )

    reported = {check.name: check for check in comparison.reported()}
    assert comparison.failures == ()
    assert not reported["barge_in_rejected"].passed
    assert "interim" in reported["barge_in_rejected"].reason


def test_a_latency_the_recording_never_measured_is_reported_not_gated() -> None:
    comparison = compare_metrics(
        {"sentence_gap_ms": {"count": 0, "p50": None, "p95": None, "max": None}},
        {"sentence_gap_ms": {"count": 3, "p50": 500.0, "p95": 500.0, "max": 500.0}},
    )

    reported = {check.name for check in comparison.reported()}
    assert comparison.failures == ()
    assert {"sentence_gap_ms.count", "sentence_gap_ms.p50"} <= reported


def test_a_latency_the_recording_measured_is_gated() -> None:
    comparison = compare_metrics(
        {"final_to_first_pcm_ms": {"count": 20, "p50": 2000.0, "p95": 4000.0, "max": 5000.0}},
        {"final_to_first_pcm_ms": {"count": 20, "p50": 2300.0, "p95": 4000.0, "max": 5000.0}},
    )

    assert not comparison.passed
    assert "final_to_first_pcm_ms.p50" in {check.name for check in comparison.failures}


def test_a_value_taken_from_the_input_must_match_exactly() -> None:
    comparison = compare_metrics(
        {"speaker_bound": {"count": 10, "of": 20, "ratio": 0.5}},
        {"speaker_bound": {"count": 10, "of": 21, "ratio": 0.5}},
    )

    assert not comparison.passed
    assert "speaker_bound.of" in {check.name for check in comparison.failures}


@pytest.mark.skipif(not (RUNTIME / "journals/live-oue-4.jsonl").exists(), reason="no oue-4 data")
def test_the_oue_4_journal_compares_against_itself() -> None:
    rows = [
        json.loads(line)
        for line in (RUNTIME / "journals/live-oue-4.jsonl").read_text().splitlines()
        if line.strip()
    ]

    comparison = compare_journals(rows, rows)

    assert comparison.passed
    assert comparison.gated()


def test_a_session_without_a_measured_reply_cannot_pass_the_gate() -> None:
    rows = [
        row("speech_started", 0.0),
        row("speech_stopped", 1.0),
        row("transcript_finalized", 1.2, transcript_id="t1"),
    ]

    comparison = compare_journals(rows, rows)

    assert not comparison.comparable
    assert not comparison.passed
