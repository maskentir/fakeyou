from datetime import timedelta
from typing import Any

from fakeyou.observability.report import (
    build_session_report,
)
from fakeyou.orchestration.runtime import (
    WRITE_ACTION_BUDGET_MAX_SECONDS,
    WRITE_ACTION_BUDGET_P95_SECONDS,
    WRITE_ACTION_STABILITY_MIN,
)
from tests._report_support import START, answered_turn, row


def meet_requested(offset: float, action: str, action_id: str, **fields: Any) -> dict[str, Any]:
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
    rows: list[dict[str, Any]] = []
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


def finished_action(
    offset: float, action: str, action_id: str, duration_ms: float
) -> list[dict[str, Any]]:
    return [
        meet_requested(offset, action, action_id),
        row(offset + 1, "meet_action_started", source="agent", action=action, action_id=action_id),
        row(
            offset + 2,
            "meet_action_observed",
            source="agent",
            action=action,
            action_id=action_id,
            observed_state="effective",
        ),
        row(
            offset + 3,
            "meet_action_finished",
            source="agent",
            action=action,
            action_id=action_id,
            observed=True,
            attempts=1,
            duration_ms=duration_ms,
        ),
    ]


def m4_stability(section: dict[str, Any]) -> float:
    finished = sum(outcomes.get("finished", 0) for outcomes in section["counts"].values())
    failed = sum(outcomes.get("failed", 0) for outcomes in section["counts"].values())
    stability: float = finished / (finished + failed)
    return stability


def test_m4_gate_passes_for_compliant_session() -> None:
    rows = (
        finished_action(0, "raise_hand", "a1", 1200)
        + finished_action(10, "send_chat", "a2", 4100)
        + finished_action(20, "raise_hand", "a3", 2500)
        + [
            meet_requested(30, "lower_hand", "a4"),
            row(
                31,
                "meet_action_rejected",
                source="agent",
                action="lower_hand",
                action_id="a4",
                reason="authority_denied",
            ),
        ]
        + answered_turn(40, "t1", "The demo is on Friday.")
        + answered_turn(60, "t2", "I will confirm the date.")
        + [
            row(
                80,
                "session_finished",
                source="cli",
                phase="ended",
                terminal_reason="stopped_by_owner",
                exit_code=0,
            )
        ]
    )
    report = build_session_report(rows)
    section = report["meet_actions"]

    assert m4_stability(section) >= WRITE_ACTION_STABILITY_MIN
    assert report["evidence"]["excluded"].get("unobserved_finish", 0) == 0
    assert section["duration_seconds"]["p95"] <= WRITE_ACTION_BUDGET_P95_SECONDS
    assert section["duration_seconds"]["max"] <= WRITE_ACTION_BUDGET_MAX_SECONDS
    assert report["latency"]["agent_to_reply_seconds"]["p95"] <= 4.0
    assert report["session_outcome"] == "completed"


def test_m4_gate_rejects_finish_without_observed_proof() -> None:
    rows = [
        meet_requested(0, "raise_hand", "a1"),
        row(
            1,
            "meet_action_finished",
            source="agent",
            action="raise_hand",
            action_id="a1",
            observed=True,
            attempts=1,
            duration_ms=1200,
        ),
    ]
    report = build_session_report(rows)

    assert report["evidence"]["excluded"].get("unobserved_finish", 0) == 1


def test_m4_gate_rejects_low_stability() -> None:
    rows: list[dict[str, Any]] = []
    for index in range(9):
        rows += finished_action(index * 10, "raise_hand", f"ok-{index}", 1200)
    rows += [
        meet_requested(100, "send_chat", "bad-1"),
        row(
            101,
            "meet_action_failed",
            source="agent",
            action="send_chat",
            action_id="bad-1",
            reason="act_failed",
            attempts=1,
            duration_ms=2000,
        ),
    ]

    assert m4_stability(build_session_report(rows)["meet_actions"]) < WRITE_ACTION_STABILITY_MIN


def test_m4_gate_rejects_agent_latency_regression() -> None:
    rows = answered_turn(0, "t1", "The demo is on Friday.")
    for entry in rows:
        if entry["event"] == "agent_finished":
            entry["at"] = (START + timedelta(seconds=10.4)).isoformat()
    report = build_session_report(rows)

    assert report["latency"]["agent_to_reply_seconds"]["p95"] == 10.0


def test_m4_gate_rejects_write_budget_breach() -> None:
    rows = finished_action(0, "send_chat", "slow-1", 13000)
    section = build_session_report(rows)["meet_actions"]

    assert section["duration_seconds"]["p95"] == 13.0
    assert section["duration_seconds"]["p95"] > WRITE_ACTION_BUDGET_P95_SECONDS


def test_meet_action_stale_cancellation_reported_as_rejection() -> None:
    rows = [
        meet_requested(0, "raise_hand", "s1"),
        row(
            1,
            "meet_action_rejected",
            source="agent",
            action="raise_hand",
            action_id="s1",
            reason="stale_cancelled",
        ),
    ]
    section = build_session_report(rows)["meet_actions"]

    assert section["rejections"] == {"stale_cancelled": 1}
    assert section["actions"] == [
        {
            "action_id": "s1",
            "action": "raise_hand",
            "outcome": "rejected",
            "reason": "stale_cancelled",
            "observed": False,
            "attempts": 1,
            "duration_ms": None,
        }
    ]
