from fakeyou.foundation.capabilities import (
    EVIDENCE_TOOLS,
    MEET_TOOLS_BY_PERMISSION,
    ONCE_PER_TURN_TOOLS,
    capability_for,
)


def test_every_meet_tool_maps_to_its_brief_permission() -> None:
    assert MEET_TOOLS_BY_PERMISSION["meet_chat"] == frozenset({"meet_send_chat"})
    assert MEET_TOOLS_BY_PERMISSION["meet_act"] == frozenset(
        {
            "meet_raise_hand",
            "meet_lower_hand",
            "meet_react",
            "meet_mute",
            "meet_unmute",
            "meet_leave",
        }
    )


def test_once_per_turn_covers_actions_and_evidence() -> None:
    assert {
        "meet_send_chat",
        "meet_raise_hand",
        "look_at_screen",
        "look_at_participant",
        "recall_meeting_context",
    } <= ONCE_PER_TURN_TOOLS
    assert "meet_check_action" not in ONCE_PER_TURN_TOOLS


def test_evidence_tools_are_the_ones_that_can_escalate() -> None:
    assert {"look_at_screen", "look_at_participant", "recall_meeting_context"} <= EVIDENCE_TOOLS
    assert "meet_send_chat" not in EVIDENCE_TOOLS
    assert "meet_react" not in EVIDENCE_TOOLS
    assert "look_back_chat" not in EVIDENCE_TOOLS


def test_unknown_tools_have_no_capability() -> None:
    assert capability_for("definitely_not_a_tool") is None
    assert capability_for("meet_leave") is not None
