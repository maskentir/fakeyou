from fakeyou.foundation.capabilities import (
    EVIDENCE_TOOLS,
    ONCE_PER_TURN_TOOLS,
    capability_for,
)


def test_once_per_turn_covers_evidence_and_action_status() -> None:
    assert {
        "look_at_screen",
        "look_at_participant",
        "recall_meeting_context",
        "meet_check_action",
    } <= ONCE_PER_TURN_TOOLS


def test_evidence_tools_are_the_ones_that_can_escalate() -> None:
    assert {"look_at_screen", "look_at_participant", "recall_meeting_context"} <= EVIDENCE_TOOLS
    assert "meet_check_action" not in EVIDENCE_TOOLS
    assert "look_back_chat" not in EVIDENCE_TOOLS


def test_meet_actions_are_not_capabilities() -> None:
    assert capability_for("definitely_not_a_tool") is None
    assert capability_for("meet_leave") is None
    assert capability_for("meet_send_chat") is None
    assert capability_for("meet_check_action") is not None
