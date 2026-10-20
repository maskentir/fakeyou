from typing import Any

from fakeyou.observability.report import build_session_report
from tests._report_support import row


def perception(offset: float, event: str, **fields: Any) -> dict[str, Any]:
    return row(offset, event, source="perception", **fields)


EMPTY_SECTION = {
    "screens": {"new": 0, "revisited": 0, "merged": 0},
    "readings": {"finished": 0, "failed": 0, "skipped": 0, "p95_ms": None},
    "views": {"emitted": 0, "with_image": 0},
    "chat_lines": 0,
    "signals": 0,
    "rejected_inputs": 0,
    "budget_exhausted": 0,
    "interpreter_disabled": 0,
}


def test_report_counts_perception() -> None:
    rows = [
        perception(0, "screen_captured", ref="screen-1", change="new"),
        perception(5, "screen_captured", ref="screen-2", change="new"),
        perception(9, "screen_captured", ref="screen-1", change="revisit"),
        perception(10, "screen_duplicate_detected", ref="screen-2", into="screen-1"),
        perception(11, "screen_reading_finished", ref="screen-1", duration_ms=1000),
        perception(12, "screen_reading_finished", ref="screen-2", duration_ms=3000),
        perception(13, "screen_reading_failed", ref="screen-3", error_type="TimeoutError"),
        perception(14, "screen_reading_skipped", ref="screen-4", reason="frame_evicted"),
        perception(15, "meeting_view_emitted", image_attached=True),
        perception(16, "meeting_view_emitted", image_attached=False),
        perception(17, "meeting_view_emitted"),
        perception(18, "chat_line_observed", ref="chat-1", chars=12, own=False),
        perception(19, "chat_line_observed", ref="chat-2", chars=4, own=True),
        perception(20, "meeting_signal_observed", kind="hand_raised", participant="person-1"),
        perception(21, "perception_input_rejected", kind="protocol", error_type="ValueError"),
        perception(22, "budget_exhausted"),
        perception(23, "perception_interpreter_disabled", error_code="config.invalid"),
    ]

    assert build_session_report(rows)["perception"] == {
        "screens": {"new": 2, "revisited": 1, "merged": 1},
        "readings": {"finished": 2, "failed": 1, "skipped": 1, "p95_ms": 2900},
        "views": {"emitted": 3, "with_image": 1},
        "chat_lines": 2,
        "signals": 1,
        "rejected_inputs": 1,
        "budget_exhausted": 1,
        "interpreter_disabled": 1,
    }


def test_empty_journal_reports_an_idle_perception_section() -> None:
    assert build_session_report([])["perception"] == EMPTY_SECTION


def test_budget_exhaustion_from_other_sources_is_not_perception() -> None:
    rows = [row(0, "budget_exhausted", source="memory"), row(1, "budget_exhausted")]

    assert build_session_report(rows)["perception"] == EMPTY_SECTION


def test_unusable_reading_durations_count_the_reading_but_not_the_latency() -> None:
    rows = [
        perception(0, "screen_reading_finished", ref="screen-1", duration_ms=-5),
        perception(1, "screen_reading_finished", ref="screen-2", duration_ms="slow"),
        perception(2, "screen_reading_finished", ref="screen-3"),
    ]

    readings = build_session_report(rows)["perception"]["readings"]

    assert (readings["finished"], readings["p95_ms"]) == (3, None)
