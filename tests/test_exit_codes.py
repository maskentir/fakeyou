from typing import get_args

from fakeyou.catalog.exit_codes import TERMINAL_EXIT_CODES, ExitCode
from fakeyou.taxonomy import TerminalReason

FAILING_REASONS = frozenset(
    {"infra_failure", "browser_failure", "audio_failure", "conversation_failure"}
)
SUCCEEDING_REASONS = frozenset(
    {
        "stopped_by_owner",
        "left_by_owner",
        "denied_by_host",
        "removed_by_host",
        "ended_by_host",
        "blocked_by_platform",
        "lobby_timeout_reached",
        "duration_limit_reached",
        "alone_limit_reached",
    }
)


def test_every_terminal_reason_has_an_explicit_exit_code() -> None:
    assert set(TERMINAL_EXIT_CODES) == set(get_args(TerminalReason))


def test_exit_code_members_have_distinct_values() -> None:
    values = [member.value for member in ExitCode]

    assert len(set(values)) == len(values)


def test_failing_terminal_reasons_are_the_documented_set() -> None:
    failing = {
        reason for reason, code in TERMINAL_EXIT_CODES.items() if code is not ExitCode.SUCCESS
    }

    assert failing == FAILING_REASONS


def test_succeeding_terminal_reasons_are_the_documented_policy() -> None:
    succeeding = {
        reason for reason, code in TERMINAL_EXIT_CODES.items() if code is ExitCode.SUCCESS
    }

    assert succeeding == SUCCEEDING_REASONS
